"""Gem Hunter V2: lightweight, read-only Solana/Base token discovery."""
from __future__ import annotations
import argparse, json, math, time
from datetime import datetime, timedelta, timezone
import requests
from .database import connect_database, storage_backend

DEX_BASE="https://api.dexscreener.com"
GECKO_BASE="https://api.geckoterminal.com/api/v2"
GECKO_MIN_INTERVAL_SECONDS=6.1
MAX_STORED=10
RETENTION_DAYS=14
MIN_LIQUIDITY_USD=10000.0
TOXIC_LIQUIDITY_USD=500.0
_HTTP=requests.Session()
_LAST_GECKO_CALL=0.0
_CEX_CACHE={"expires":0.0,"symbols":{}}

def _now():
    return datetime.now(timezone.utc)

def _iso(value=None):
    return (value or _now()).astimezone(timezone.utc).isoformat().replace("+00:00","Z")

def _number(value):
    if value is None or isinstance(value,bool):
        return None
    try:
        result=float(value)
    except (TypeError,ValueError,OverflowError):
        return None
    return result if math.isfinite(result) else None

def _get_json(url,*,params=None,timeout=15):
    response=_HTTP.get(url,params=params,timeout=timeout,headers={"Accept":"application/json","User-Agent":"CryptoSuperRadar-GemHunterV2/1.0"})
    response.raise_for_status()
    payload=response.json()
    if not isinstance(payload,(dict,list)):
        raise ValueError("Unexpected API response type")
    return payload

def _gecko_pool(chain,token,pair):
    global _LAST_GECKO_CALL
    delay=GECKO_MIN_INTERVAL_SECONDS-(time.monotonic()-_LAST_GECKO_CALL)
    if delay>0:
        time.sleep(delay)
    _LAST_GECKO_CALL=time.monotonic()
    payload=_get_json(f"{GECKO_BASE}/networks/{chain}/tokens/{token}/pools",params={"page":1})
    for row in payload.get("data",[]):
        attrs=row.get("attributes") or {}
        if str(attrs.get("address") or "").lower()==pair.lower():
            return attrs
    return None

def _security(chain,token):
    """Read-only security evidence; missing fields remain NOT_AVAILABLE."""
    try:
        if chain=="base":
            payload=_get_json("https://api.gopluslabs.io/api/v1/token_security/8453",params={"contract_addresses":token},timeout=12)
            result=(payload.get("result") or {}).get(token.lower()) or {}
            if not result:
                return {"status":"NOT_AVAILABLE","provider":"GOPLUS","reason":"No security record."}
            critical={"is_honeypot":"Honeypot","is_mintable":"Mint authority","cannot_sell_all":"Sell restriction","is_blacklisted":"Blacklist","transfer_pausable":"Transfer pause","owner_change_balance":"Owner balance modification","hidden_owner":"Hidden owner"}
            flags={key:result.get(key) for key in critical}
            failures=[label for key,label in critical.items() if str(result.get(key,""))=="1"]
            if failures:
                return {"status":"FAIL","provider":"GOPLUS","flags":flags,"reason":"Critical risk: "+", ".join(failures)}
            required=(*critical.keys(),"is_open_source")
            if any(key not in result for key in required) or str(result.get("is_open_source"))!="1":
                return {"status":"NOT_AVAILABLE","provider":"GOPLUS","flags":flags,"reason":"Mandatory security evidence incomplete."}
            return {"status":"PASS","provider":"GOPLUS","flags":flags,"reason":"Minimum GoPlus checks passed; not a full audit."}
        if chain=="solana":
            response=_HTTP.post("https://api.mainnet-beta.solana.com",json={"jsonrpc":"2.0","id":1,"method":"getAccountInfo","params":[token,{"encoding":"jsonParsed","commitment":"confirmed"}]},timeout=12,headers={"Content-Type":"application/json"})
            response.raise_for_status()
            info=(response.json().get("result") or {}).get("value") or {}
            parsed=((info.get("data") or {}).get("parsed") or {}).get("info") or {}
            if not parsed:
                return {"status":"NOT_AVAILABLE","provider":"SOLANA_RPC","reason":"Parsed mint unavailable."}
            mint,freeze=parsed.get("mintAuthority"),parsed.get("freezeAuthority")
            if mint or freeze:
                return {"status":"FAIL","provider":"SOLANA_RPC","mint_authority_active":bool(mint),"freeze_authority_active":bool(freeze),"reason":"Active Mint or Freeze authority detected."}
            return {"status":"NOT_AVAILABLE","provider":"SOLANA_RPC","mint_authority_active":False,"freeze_authority_active":False,"reason":"Mint/Freeze revoked, but sellability simulation unavailable."}
    except Exception as exc:
        return {"status":"NOT_AVAILABLE","provider":"PUBLIC_SECURITY_API","reason":type(exc).__name__}
    return {"status":"NOT_AVAILABLE","provider":"UNSUPPORTED"}

def _holders(chain,token):
    return {"status":"NOT_AVAILABLE","top10_pct":None,"provider":"NOT_CONFIGURED"}

def _cex_symbols():
    now=time.monotonic()
    if _CEX_CACHE["expires"]>now:
        return _CEX_CACHE["symbols"]
    endpoints={"BINANCE":"https://api.binance.com/api/v3/exchangeInfo","BYBIT":"https://api.bybit.com/v5/market/instruments-info","KRAKEN":"https://api.kraken.com/0/public/AssetPairs","OKX":"https://www.okx.com/api/v5/public/instruments"}
    found={}
    for name,url in endpoints.items():
        try:
            if name=="BYBIT":
                rows=(_get_json(url,params={"category":"spot","limit":1000}).get("result") or {}).get("list",[])
            elif name=="OKX":
                rows=_get_json(url,params={"instType":"SPOT"}).get("data",[])
            elif name=="KRAKEN":
                rows=list(_get_json(url).get("result",{}).values())
            else:
                rows=_get_json(url).get("symbols",[])
            symbols=set()
            for row in rows:
                if not isinstance(row,dict):
                    continue
                state=str(row.get("status") or row.get("state") or "TRADING").upper()
                base=str(row.get("baseAsset") or row.get("baseCoin") or row.get("baseCcy") or row.get("base") or "").upper()
                quote=str(row.get("quoteAsset") or row.get("quoteCoin") or row.get("quoteCcy") or row.get("quote") or "").upper()
                if state in {"TRADING","LIVE","ONLINE"} and base and quote in {"USDT","USDC","USD","EUR","BTC","ETH"}:
                    symbols.add(base)
            found[name]=symbols
        except Exception:
            found[name]=set()
    _CEX_CACHE.update({"expires":now+1800,"symbols":found})
    return found

def _cex_listing(symbol,symbols):
    symbol=str(symbol or "").upper()
    exchanges=sorted(name for name,listed in symbols.items() if symbol and symbol in listed)
    return {"status":"CEX_AVAILABLE" if exchanges else "DEX_ONLY","exchanges":exchanges,"match_type":"SYMBOL_ONLY","warning":"Ticker match only; contract identity, Germany account access and MiCA authorization are not implied."}

def _score(candidate,previous=None):
    security=candidate.get("security") or {}
    liquidity=_number(candidate.get("liquidity_usd"))
    buys,sells=_number(candidate.get("buys_1h")),_number(candidate.get("sells_1h"))
    volume_1h,volume_24h=_number(candidate.get("volume_1h_usd")),_number(candidate.get("volume_24h_usd"))
    age=_number(candidate.get("pool_age_hours"))
    parts={"contract_security":25 if security.get("status")=="PASS" else 0,"liquidity":0,"buy_sell_momentum":0,"holder_distribution":0,"age_activity":0,"pool_quality":0}
    hard=[]
    if security.get("status")=="FAIL":
        hard.append(security.get("reason") or "Critical contract risk.")
    if liquidity is not None and liquidity<TOXIC_LIQUIDITY_USD:
        hard.append("Toxic or near-zero pool liquidity.")
    if liquidity is not None and liquidity>=MIN_LIQUIDITY_USD:
        parts["liquidity"]=12
        if previous and _number(previous.get("liquidity_usd")):
            old=float(previous["liquidity_usd"])
            delta=abs(liquidity-old)/max(old,1.0)
            parts["liquidity"]+=8 if delta<=.20 else 4 if delta<=.40 else 0
        else:
            parts["liquidity"]+=3
    elif liquidity is not None and liquidity>=2500:
        parts["liquidity"]=6
    if buys is not None and sells is not None and buys+sells>0:
        ratio=(buys+1.0)/(sells+1.0)
        parts["buy_sell_momentum"]=(15 if ratio>=2 and buys+sells>=30 else 11 if ratio>=1.3 and buys+sells>=15 else 7 if ratio>=1 and buys+sells>=8 else 3 if ratio>=.75 else 0)
    velocity=(volume_1h*24/volume_24h) if volume_1h is not None and volume_24h and volume_24h>0 else None
    if velocity is not None:
        parts["buy_sell_momentum"]+=5 if velocity>=1.5 else 3 if velocity>=1.0 else 0
    holders=candidate.get("holders") or {}
    top10=_number(holders.get("top10_pct"))
    if holders.get("status")=="AVAILABLE" and top10 is not None:
        parts["holder_distribution"]=15 if top10<=25 else 10 if top10<=40 else 5 if top10<=60 else 0
    if age is not None:
        parts["age_activity"]=10 if age<=24 else 8 if age<=72 else 6 if age<=168 else 2
        if buys is not None and sells is not None and buys+sells>=20:
            parts["age_activity"]=min(10,parts["age_activity"]+1)
    if candidate.get("gecko_match") is True:
        parts["pool_quality"]=10
    score=sum(parts.values())
    if hard:
        status="REJECT"
    elif security.get("status")!="PASS" or liquidity is None or liquidity<MIN_LIQUIDITY_USD or candidate.get("gecko_match") is not True or score<70:
        status="WATCH"
    elif score>=85:
        status="STRONG GEM" if int(candidate.get("consecutive_scans") or 1)>=2 else "WATCH"
    else:
        status="EARLY GEM"
    return {"score":score,"score_parts":parts,"status":status,"hard_reject_reasons":hard,"buy_sell_ratio":round((buys+1)/(sells+1),3) if buys is not None and sells is not None else None,"volume_velocity_ratio":round(velocity,3) if velocity is not None else None}

def discover_and_score(limit_per_chain=10):
    profiles=_get_json(f"{DEX_BASE}/token-profiles/latest/v1")
    profiles=profiles if isinstance(profiles,list) else []
    cex,rows,counts=_cex_symbols(),{},{"solana":0,"base":0}
    limit=max(1,min(int(limit_per_chain),10))
    for profile in profiles:
        if not isinstance(profile,dict):
            continue
        chain,token=str(profile.get("chainId") or "").lower(),str(profile.get("tokenAddress") or "")
        if chain not in counts or not token or counts[chain]>=limit:
            continue
        try:
            pairs=_get_json(f"{DEX_BASE}/token-pairs/v1/{chain}/{token}")
        except Exception:
            continue
        eligible=sorted((p for p in pairs if isinstance(p,dict) and str(p.get("chainId") or "").lower()==chain),key=lambda p:_number((p.get("liquidity") or {}).get("usd")) or 0,reverse=True)
        for pair in eligible[:2]:
            base=pair.get("baseToken") or {}
            if str(base.get("address") or "").lower()!=token.lower():
                continue
            try:
                gecko=_gecko_pool(chain,token,str(pair.get("pairAddress") or ""))
            except Exception:
                gecko=None
            liq=_number((pair.get("liquidity") or {}).get("usd"))
            gecko_liq=_number((gecko or {}).get("reserve_in_usd"))
            match=bool(gecko is not None and liq is not None and gecko_liq is not None and abs(liq-gecko_liq)/max(liq,gecko_liq,1.0)<=.25)
            tx=pair.get("txns") or {}
            h1,h24=tx.get("h1") or {},tx.get("h24") or {}
            created=_number(pair.get("pairCreatedAt"))
            age=max(0.0,(_now().timestamp()-created/1000)/3600) if created else None
            candidate={"candidate_id":f"{chain}:{token.lower()}:{str(pair.get('pairAddress') or '').lower()}","chain":chain,"token_address":token,"token_name":base.get("name"),"token_symbol":base.get("symbol"),"pool_address":pair.get("pairAddress"),"dex":pair.get("dexId"),"price_usd":_number(pair.get("priceUsd")),"liquidity_usd":liq,"volume_1h_usd":_number((pair.get("volume") or {}).get("h1")),"volume_24h_usd":_number((pair.get("volume") or {}).get("h24")),"buys_1h":_number(h1.get("buys")),"sells_1h":_number(h1.get("sells")),"buys_24h":_number(h24.get("buys")),"sells_24h":_number(h24.get("sells")),"pool_age_hours":round(age,2) if age is not None else None,"gecko_match":match,"gecko_liquidity_usd":gecko_liq,"security":_security(chain,token),"holders":_holders(chain,token),"cex":_cex_listing(base.get("symbol"),cex),"sources":["DEX_SCREENER"]+(["GECKOTERMINAL"] if gecko else []),"observed_at_utc":_iso(),"trade_authorization":"NO_TRADE","read_only":True}
            rows[candidate["candidate_id"]]=candidate
            counts[chain]+=1
            break
    return sorted(rows.values(),key=lambda x:x.get("liquidity_usd") or 0,reverse=True)[:MAX_STORED]

def initialize_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS gem_hunter_v2_candidates (candidate_id TEXT PRIMARY KEY,chain TEXT NOT NULL,token_address TEXT NOT NULL,pool_address TEXT NOT NULL,score INTEGER NOT NULL,status TEXT NOT NULL,consecutive_scans INTEGER NOT NULL,liquidity_usd REAL,payload_json TEXT NOT NULL,first_seen_utc TEXT NOT NULL,last_seen_utc TEXT NOT NULL,expires_at_utc TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_gem_hunter_v2_expiry ON gem_hunter_v2_candidates(expires_at_utc)")

def save_top10(candidates,*,connection=None,now=None):
    own,db=connection is None,connection or connect_database()
    now=now or _now()
    stamp,expires=_iso(now),_iso(now+timedelta(days=RETENTION_DAYS))
    try:
        initialize_schema(db)
        prepared=[]
        for item in candidates:
            old=db.execute("SELECT payload_json,first_seen_utc,last_seen_utc FROM gem_hunter_v2_candidates WHERE candidate_id=?",(item["candidate_id"],)).fetchone()
            previous=json.loads(old["payload_json"]) if old else None
            consecutive,first_seen=1,stamp
            if old:
                first_seen=old["first_seen_utc"]
                try:
                    last=datetime.fromisoformat(str(old["last_seen_utc"]).replace("Z","+00:00"))
                    if (now-last).total_seconds()<=5*3600:
                        consecutive=int(previous.get("consecutive_scans") or 1)+1
                except Exception:
                    consecutive=1
            item["consecutive_scans"]=consecutive
            item.update(_score(item,previous))
            item["first_seen_utc"],item["last_seen_utc"],item["expires_at_utc"]=first_seen,stamp,expires
            item["market_access"]=(item.get("cex") or {}).get("status","NOT_VERIFIED")
            item["eu_mica_warning"]="CEX listing is not MiCA authorization. Verify the legal entity, Germany access and token availability separately."
            prepared.append(item)
        prepared.sort(key=lambda x:(x["status"]!="REJECT",x["score"]),reverse=True)
        prepared=prepared[:MAX_STORED]
        for item in prepared:
            db.execute("""INSERT INTO gem_hunter_v2_candidates (candidate_id,chain,token_address,pool_address,score,status,consecutive_scans,liquidity_usd,payload_json,first_seen_utc,last_seen_utc,expires_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET score=excluded.score,status=excluded.status,consecutive_scans=excluded.consecutive_scans,liquidity_usd=excluded.liquidity_usd,payload_json=excluded.payload_json,last_seen_utc=excluded.last_seen_utc,expires_at_utc=excluded.expires_at_utc""",(item["candidate_id"],item["chain"],item["token_address"],item["pool_address"],item["score"],item["status"],item["consecutive_scans"],item.get("liquidity_usd"),json.dumps(item,ensure_ascii=False,separators=(",",":")),item["first_seen_utc"],item["last_seen_utc"],item["expires_at_utc"]))
        db.execute("""DELETE FROM gem_hunter_v2_candidates WHERE candidate_id NOT IN (SELECT candidate_id FROM gem_hunter_v2_candidates ORDER BY score DESC,last_seen_utc DESC LIMIT 10)""")
        db.execute("DELETE FROM gem_hunter_v2_candidates WHERE expires_at_utc<?",(stamp,))
        db.commit()
        return prepared
    finally:
        if own:
            db.close()

def public_snapshot(*,connection=None):
    own,db=connection is None,connection
    try:
        if db is None:
            if storage_backend()!="TURSO_CLOUD_SQLITE":
                return {"status":"NOT_CONFIGURED","candidates":[],"message_fa":"اتصال Turso برای Gem Hunter V2 تنظیم نشده است.","read_only":True,"trade_authorization":"NO_TRADE"}
            db=connect_database()
        rows=db.execute("SELECT payload_json FROM gem_hunter_v2_candidates WHERE expires_at_utc>=? ORDER BY score DESC,last_seen_utc DESC LIMIT 10",(_iso(),)).fetchall()
        candidates=[json.loads(row["payload_json"]) for row in rows]
        return {"status":"OK","generated_at_utc":_iso(),"candidate_count":len(candidates),"candidates":candidates,"read_only":True,"trade_authorization":"NO_TRADE","scope":"GEM_HUNTER_V2_ONLY"}
    except Exception as exc:
        detail = str(exc).lower()
        if "no such table" in detail:
            error_code = "SCHEMA_MISSING"
        elif "unauthorized" in detail or "401" in detail or "authentication" in detail:
            error_code = "AUTH_FAILED"
        elif "connect" in detail or "timeout" in detail or "network" in detail:
            error_code = "CONNECTION_FAILED"
        else:
            error_code = "DATABASE_OPERATION_FAILED"
        return {"status":"UNAVAILABLE","candidates":[],"error_type":type(exc).__name__,
                "error_code":error_code,"read_only":True,"trade_authorization":"NO_TRADE"}
    finally:
        if own and db is not None:
            db.close()

def run_scan():
    if storage_backend()!="TURSO_CLOUD_SQLITE":
        return {"status":"SKIPPED","reason":"Turso credentials are not configured.","trade_authorization":"NO_TRADE"}
    saved=save_top10(discover_and_score())
    return {"status":"OK","discovered":len(saved),"saved":len(saved),"top_statuses":{s:sum(1 for x in saved if x["status"]==s) for s in ("EARLY GEM","STRONG GEM","WATCH","REJECT")},"trade_authorization":"NO_TRADE"}

def main():
    parser=argparse.ArgumentParser(description="Read-only Gem Hunter V2 scan")
    parser.add_argument("--scan",action="store_true")
    args=parser.parse_args()
    if not args.scan:
        parser.print_help()
        return 0
    print(json.dumps(run_scan(),ensure_ascii=False))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
