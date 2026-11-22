import sqlite3
import unittest
from datetime import datetime,timezone
from crypto_super_radar.gem_hunter_v2 import _score,initialize_schema,save_top10,ingest_candidates,public_snapshot

class GemHunterV2Tests(unittest.TestCase):
    def candidate(self,**overrides):
        row={"candidate_id":"base:0xabc:pool","chain":"base","token_address":"0xabc","pool_address":"pool","token_symbol":"TST","liquidity_usd":50000,"volume_24h_usd":80000,"buys_1h":80,"sells_1h":20,"pool_age_hours":8,"gecko_match":True,"security":{"status":"PASS","provider":"TEST"},"holders":{"status":"NOT_AVAILABLE","top10_pct":None},"cex":{"status":"DEX_ONLY","exchanges":[]},"read_only":True,"trade_authorization":"NO_TRADE"}
        row.update(overrides)
        return row
    def test_security_failure_is_hard_reject(self):
        result=_score(self.candidate(security={"status":"FAIL","reason":"honeypot"}))
        self.assertEqual(result["status"],"REJECT")
        self.assertIn("honeypot",result["hard_reject_reasons"][0])
    def test_missing_security_is_watch(self):
        self.assertEqual(_score(self.candidate(security={"status":"NOT_AVAILABLE"}))["status"],"WATCH")
    def test_low_liquidity_is_watch(self):
        self.assertEqual(_score(self.candidate(liquidity_usd=7000))["status"],"WATCH")
    def test_missing_holder_provider_does_not_crash(self):
        result=_score(self.candidate())
        self.assertGreaterEqual(result["score"],70)
        self.assertEqual(result["status"],"EARLY GEM")
    def test_strong_gem_requires_repeated_scan(self):
        row=self.candidate(holders={"status":"AVAILABLE","top10_pct":18},liquidity_usd=200000,buys_1h=100,sells_1h=20,consecutive_scans=1)
        self.assertNotEqual(_score(row)["status"],"STRONG GEM")
        row["consecutive_scans"]=2
        self.assertEqual(_score(row)["status"],"STRONG GEM")
    def test_toxic_liquidity_is_reject(self):
        self.assertEqual(_score(self.candidate(liquidity_usd=100))["status"],"REJECT")
    def test_ingest_preserves_scan_count_and_is_idempotent(self):
        db=sqlite3.connect(":memory:")
        db.row_factory=sqlite3.Row
        initialize_schema(db)
        row=self.candidate(score=90,status="STRONG GEM",consecutive_scans=2,first_seen_utc="2026-11-22T00:00:00Z",last_seen_utc="2026-11-22T02:00:00Z",expires_at_utc="2026-12-06T02:00:00Z")
        first=ingest_candidates([row],connection=db,now=datetime(2026,11,22,2,1,tzinfo=timezone.utc))
        second=ingest_candidates([row],connection=db,now=datetime(2026,11,22,2,2,tzinfo=timezone.utc))
        self.assertEqual(first[0]["consecutive_scans"],2)
        self.assertEqual(second[0]["consecutive_scans"],2)
        stored=db.execute("SELECT payload_json FROM gem_hunter_v2_candidates WHERE candidate_id=?",(row["candidate_id"],)).fetchone()
        self.assertEqual(__import__("json").loads(stored["payload_json"])["status"],"STRONG GEM")
        db.close()
    def test_ingest_rejects_non_read_only_payload(self):
        db=sqlite3.connect(":memory:")
        db.row_factory=sqlite3.Row
        initialize_schema(db)
        row=self.candidate(score=75,status="EARLY GEM",trade_authorization="ORDER")
        with self.assertRaises(ValueError):
            ingest_candidates([row],connection=db)
        db.close()
    def test_top10_storage_and_public_read_only(self):
        db=sqlite3.connect(":memory:")
        db.row_factory=sqlite3.Row
        initialize_schema(db)
        rows=[self.candidate(candidate_id=f"base:{i}:pool",token_address=f"0x{i}",pool_address=f"pool{i}",liquidity_usd=50000+i*1000,buys_1h=80+i,sells_1h=20) for i in range(12)]
        saved=save_top10(rows,connection=db,now=datetime(2026,11,22,tzinfo=timezone.utc))
        self.assertEqual(len(saved),10)
        snap=public_snapshot(connection=db)
        self.assertEqual(snap["candidate_count"],10)
        self.assertTrue(snap["read_only"])
        self.assertEqual(snap["trade_authorization"],"NO_TRADE")
        db.close()

if __name__=="__main__":
    unittest.main()
