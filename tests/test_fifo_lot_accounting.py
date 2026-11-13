from __future__ import annotations

import sqlite3
import unittest

from crypto_super_radar.fifo_lot_accounting import FIFOLotAccounting
from crypto_super_radar.multi_currency_ledger import (
    LedgerIdempotencyConflict,
    LedgerValidationError,
    MultiCurrencyPaperLedger,
)


class FIFOLotAccountingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.ledger = MultiCurrencyPaperLedger(self.db)
            self.ledger.create_portfolio("fifo-usd", "USD", created_at=1_800_000_000)
            self.ledger.record_capital_contribution(
                "fifo-usd", idempotency_key="opening-capital",
                amount=10_000, occurred_at=1_800_000_001,
            )
            self.fifo = FIFOLotAccounting(self.db, self.ledger)

    def tearDown(self):
        self.db.close()

    def buy(self, key, qty, price, at, **fx):
        with self.db:
            return self.fifo.record_buy(
                "fifo-usd", idempotency_key=key, instrument_id="cex:BTCUSDT",
                quantity=qty, unit_price=price, quote_currency=fx.pop("quote_currency", "USD"),
                occurred_at=at, **fx,
            )

    def sell(self, key, qty, price, at, **fx):
        with self.db:
            return self.fifo.record_sell(
                "fifo-usd", idempotency_key=key, instrument_id="cex:BTCUSDT",
                quantity=qty, unit_price=price, quote_currency=fx.pop("quote_currency", "USD"),
                occurred_at=at, **fx,
            )

    def test_fifo_consumes_oldest_lot_and_calculates_realized_gain(self):
        first = self.buy("buy-1", 1.0, 100.0, 1_800_000_010)
        second = self.buy("buy-2", 1.0, 150.0, 1_800_000_020)
        self.assertTrue(first["inserted"])
        self.assertTrue(second["inserted"])

        sale = self.sell("sell-1", 1.5, 200.0, 1_800_000_030)
        self.assertEqual(sale["method"], "FIFO")
        self.assertEqual([x["lot_id"] for x in sale["allocations"]], [
            first["lot_id"], second["lot_id"]
        ])
        self.assertAlmostEqual(sale["cost_basis_base"], 175.0)
        self.assertAlmostEqual(sale["proceeds_base"], 300.0)
        self.assertAlmostEqual(sale["realized_pnl_base"], 125.0)

        lots = self.fifo.open_lots("fifo-usd", "cex:BTCUSDT")
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]["lot_id"], second["lot_id"])
        self.assertAlmostEqual(lots[0]["remaining_quantity"], 0.5)
        self.assertEqual(self.ledger.trial_balance("fifo-usd")["status"], "PASS")
        self.assertEqual(self.ledger.replay("fifo-usd")["status"], "PASS")
        self.assertEqual(self.fifo.replay("fifo-usd")["status"], "PASS")

    def test_realized_loss_is_debited_to_expense_account(self):
        self.buy("buy-loss", 2.0, 100.0, 1_800_000_010)
        sale = self.sell("sell-loss", 1.0, 80.0, 1_800_000_020)
        self.assertAlmostEqual(sale["cost_basis_base"], 100.0)
        self.assertAlmostEqual(sale["proceeds_base"], 80.0)
        self.assertAlmostEqual(sale["realized_pnl_base"], -20.0)
        balances = self.ledger.account_balances("fifo-usd")
        loss = next(x for x in balances if x["account_code"] == "EXPENSE:REALIZED_PNL")
        self.assertAlmostEqual(loss["book_value_base"], 20.0)
        self.assertEqual(self.ledger.trial_balance("fifo-usd")["status"], "PASS")

    def test_foreign_currency_cost_basis_uses_acquisition_fx_not_sale_fx(self):
        with self.db:
            self.ledger.record_capital_contribution(
                "fifo-usd", idempotency_key="eur-cash", amount=1_000,
                currency="EUR", fx_rate_to_base=1.1, fx_source="ECB",
                fx_observed_at=1_800_000_009, fx_quality="OBSERVED",
                occurred_at=1_800_000_010,
            )
        self.buy(
            "buy-eur", 2.0, 100.0, 1_800_000_011,
            quote_currency="EUR", fx_rate_to_base=1.1,
            fx_source="ECB", fx_observed_at=1_800_000_009, fx_quality="OBSERVED",
        )
        sale = self.sell(
            "sell-usd", 1.0, 130.0, 1_800_000_020,
            quote_currency="USD",
        )
        self.assertAlmostEqual(sale["cost_basis_base"], 110.0)
        self.assertAlmostEqual(sale["proceeds_base"], 130.0)
        self.assertAlmostEqual(sale["realized_pnl_base"], 20.0)
        self.assertEqual(self.ledger.trial_balance("fifo-usd")["status"], "PASS")

    def test_oversell_is_rejected_without_journal_or_allocation_write(self):
        self.buy("buy-small", 0.25, 100.0, 1_800_000_010)
        before = len(self.ledger.recent_transactions("fifo-usd", limit=50))
        with self.assertRaisesRegex(LedgerValidationError, "insufficient open FIFO inventory"):
            self.sell("sell-too-much", 0.5, 120.0, 1_800_000_020)
        after = len(self.ledger.recent_transactions("fifo-usd", limit=50))
        self.assertEqual(before, after)
        self.assertEqual(self.fifo.replay("fifo-usd")["allocation_count"], 0)

    def test_sale_retry_is_idempotent_and_changed_request_conflicts(self):
        self.buy("buy-retry", 1.0, 100.0, 1_800_000_010)
        first = self.sell("sell-retry", 0.5, 120.0, 1_800_000_020)
        repeated = self.sell("sell-retry", 0.5, 120.0, 1_800_000_020)
        self.assertTrue(first["inserted"])
        self.assertFalse(repeated["inserted"])
        self.assertEqual(first["transaction_id"], repeated["transaction_id"])
        self.assertAlmostEqual(repeated["realized_pnl_base"], 10.0)
        with self.assertRaises(LedgerIdempotencyConflict):
            self.sell("sell-retry", 0.4, 120.0, 1_800_000_020)

    def test_buy_retry_does_not_create_duplicate_lot(self):
        first = self.buy("buy-repeat", 1.0, 100.0, 1_800_000_010)
        repeated = self.buy("buy-repeat", 1.0, 100.0, 1_800_000_010)
        self.assertTrue(first["inserted"])
        self.assertFalse(repeated["inserted"])
        self.assertEqual(len(self.fifo.open_lots("fifo-usd")), 1)

    def test_fifo_lot_tables_are_append_only(self):
        self.buy("buy-immutable", 1.0, 100.0, 1_800_000_010)
        lot = self.fifo.open_lots("fifo-usd")[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE paper_cost_basis_lots SET quantity=2 WHERE lot_id=?",
                (lot["lot_id"],),
            )
        self.sell("sell-immutable", 0.5, 120.0, 1_800_000_020)
        allocation = self.db.execute(
            "SELECT allocation_id FROM paper_cost_basis_allocations"
        ).fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "DELETE FROM paper_cost_basis_allocations WHERE allocation_id=?",
                (allocation["allocation_id"],),
            )

    def test_foreign_currency_requires_explicit_fx_provenance(self):
        with self.assertRaisesRegex(LedgerValidationError, "FX source"):
            self.buy(
                "buy-missing-fx", 1.0, 100.0, 1_800_000_010,
                quote_currency="EUR", fx_rate_to_base=1.1,
                fx_observed_at=1_800_000_009,
            )


if __name__ == "__main__":
    unittest.main()
