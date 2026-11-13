from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from crypto_super_radar.multi_currency_ledger import (
    LedgerIdempotencyConflict,
    LedgerValidationError,
    MultiCurrencyPaperLedger,
    PortfolioConfigurationConflict,
)


class MultiCurrencyPaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.ledger = MultiCurrencyPaperLedger(self.db)
            self.ledger.create_portfolio(
                "paper-usd", "USD", created_at=1_800_000_000
            )

    def tearDown(self):
        self.db.close()

    @staticmethod
    def identity_line(account, instrument, quantity, *, unit_price=1.0, currency="USD"):
        return {
            "account_code": account,
            "instrument_id": instrument,
            "quantity": quantity,
            "unit_price": unit_price,
            "quote_currency": currency,
            "fx_rate_to_base": 1.0,
            "fx_quality": "IDENTITY",
            "fx_source": "IDENTITY",
            "fx_observed_at": 1_800_000_000,
        }

    def test_portfolio_base_currency_is_immutable_and_creation_is_idempotent(self):
        repeated = self.ledger.create_portfolio(
            "paper-usd", "USD", created_at=1_800_000_100
        )
        self.assertFalse(repeated["inserted"])
        self.assertEqual(repeated["created_at"], 1_800_000_000)
        with self.assertRaises(PortfolioConfigurationConflict):
            self.ledger.create_portfolio(
                "paper-usd", "EUR", created_at=1_800_000_100
            )

    def test_opening_capital_is_double_entry_and_reconciles(self):
        with self.db:
            result = self.ledger.record_capital_contribution(
                "paper-usd",
                idempotency_key="opening-capital-2026",
                amount=10_000,
                occurred_at=1_800_000_010,
            )
        self.assertTrue(result["inserted"])
        balances = self.ledger.account_balances("paper-usd")
        cash = next(row for row in balances if row["account_code"] == "ASSET:CASH:USD")
        capital = next(row for row in balances if row["account_code"] == "EQUITY:CAPITAL")
        self.assertEqual(cash["quantity"], 10_000.0)
        self.assertEqual(cash["book_value_base"], 10_000.0)
        self.assertEqual(capital["book_value_base"], -10_000.0)
        self.assertEqual(self.ledger.trial_balance("paper-usd")["status"], "PASS")

    def test_cross_currency_conversion_and_european_equity_position(self):
        with self.db:
            self.ledger.record_capital_contribution(
                "paper-usd",
                idempotency_key="capital-usd",
                amount=10_000,
                occurred_at=1_800_000_010,
            )
            conversion = self.ledger.post_transaction(
                portfolio_id="paper-usd",
                idempotency_key="usd-to-eur-001",
                transaction_type="FX_CONVERSION",
                occurred_at=1_800_000_020,
                source="PAPER_FX_SIMULATOR",
                entries=[
                    {
                        "account_code": "ASSET:CASH:EUR",
                        "instrument_id": "cash:EUR",
                        "quantity": 900,
                        "unit_price": 1,
                        "quote_currency": "EUR",
                        "fx_rate_to_base": 1.1,
                        "fx_source": "ECB_REFERENCE",
                        "fx_observed_at": 1_800_000_019,
                        "fx_quality": "OBSERVED",
                    },
                    self.identity_line(
                        "ASSET:CASH:USD", "cash:USD", -990
                    ),
                ],
            )
            trade = self.ledger.post_transaction(
                portfolio_id="paper-usd",
                idempotency_key="eu-sap-paper-buy-001",
                transaction_type="TRADE",
                occurred_at=1_800_000_030,
                source="PAPER_FILL_SIMULATOR",
                entries=[
                    {
                        "account_code": "ASSET:INVENTORY:EU:SAP",
                        "instrument_id": "eu:sap",
                        "quantity": 5,
                        "unit_price": 180,
                        "quote_currency": "EUR",
                        "fx_rate_to_base": 1.1,
                        "fx_source": "ECB_REFERENCE",
                        "fx_observed_at": 1_800_000_029,
                        "fx_quality": "OBSERVED",
                    },
                    {
                        "account_code": "ASSET:CASH:EUR",
                        "instrument_id": "cash:EUR",
                        "quantity": -900,
                        "unit_price": 1,
                        "quote_currency": "EUR",
                        "fx_rate_to_base": 1.1,
                        "fx_source": "ECB_REFERENCE",
                        "fx_observed_at": 1_800_000_029,
                        "fx_quality": "OBSERVED",
                    },
                ],
            )

        self.assertTrue(conversion["inserted"])
        self.assertTrue(trade["inserted"])
        balances = self.ledger.account_balances("paper-usd")
        eur_cash = [
            row for row in balances
            if row["account_code"] == "ASSET:CASH:EUR"
        ]
        self.assertEqual(eur_cash, [])
        inventory = next(
            row for row in balances
            if row["account_code"] == "ASSET:INVENTORY:EU:SAP"
        )
        usd_cash = next(
            row for row in balances
            if row["account_code"] == "ASSET:CASH:USD"
        )
        self.assertEqual(inventory["quantity"], 5.0)
        self.assertEqual(inventory["book_value_base"], 990.0)
        self.assertEqual(usd_cash["quantity"], 9_010.0)
        trial = self.ledger.trial_balance("paper-usd")
        self.assertEqual(trial["status"], "PASS")
        self.assertEqual(trial["base_currency"], "USD")
        self.assertFalse(trial["mark_to_market"])

    def test_multiple_portfolios_can_use_different_base_currencies(self):
        with self.db:
            self.ledger.create_portfolio(
                "paper-eur", "EUR", created_at=1_800_000_000
            )
            self.ledger.record_capital_contribution(
                "paper-eur",
                idempotency_key="eur-opening-capital",
                amount=500,
                occurred_at=1_800_000_010,
            )
            self.ledger.record_capital_contribution(
                "paper-eur",
                idempotency_key="usd-contribution-to-eur-book",
                amount=100,
                currency="USD",
                fx_rate_to_base=0.92,
                fx_source="ECB_REFERENCE",
                fx_observed_at=1_800_000_009,
                fx_quality="OBSERVED",
                occurred_at=1_800_000_010,
            )
        trial = self.ledger.trial_balance("paper-eur")
        self.assertEqual(trial["status"], "PASS")
        self.assertEqual(trial["base_currency"], "EUR")
        usd_cash = next(
            row for row in self.ledger.account_balances("paper-eur")
            if row["instrument_id"] == "cash:USD"
        )
        self.assertEqual(usd_cash["quantity"], 100.0)
        self.assertEqual(usd_cash["book_value_base"], 92.0)
        self.assertEqual(self.ledger.trial_balance("paper-usd")["debits_base"], 0.0)

    def test_unbalanced_transaction_is_rejected_before_writing(self):
        with self.assertRaisesRegex(LedgerValidationError, "not balanced"):
            with self.db:
                self.ledger.post_transaction(
                    portfolio_id="paper-usd",
                    idempotency_key="bad-unbalanced",
                    transaction_type="TRADE",
                    occurred_at=1_800_000_040,
                    source="TEST",
                    entries=[
                        self.identity_line(
                            "ASSET:INVENTORY:CRYPTO:BTC", "crypto:BTCUSDT", 1,
                            unit_price=100,
                        ),
                        self.identity_line(
                            "ASSET:CASH:USD", "cash:USD", -99,
                        ),
                    ],
                )
        self.assertEqual(self.ledger.replay("paper-usd")["transaction_count"], 0)

    def test_foreign_currency_requires_timestamped_fx_provenance(self):
        with self.assertRaisesRegex(LedgerValidationError, "FX quality"):
            with self.db:
                self.ledger.post_transaction(
                    portfolio_id="paper-usd",
                    idempotency_key="missing-fx-proof",
                    transaction_type="FX_CONVERSION",
                    occurred_at=1_800_000_050,
                    source="TEST",
                    entries=[
                        {
                            "account_code": "ASSET:CASH:EUR",
                            "instrument_id": "cash:EUR",
                            "quantity": 10,
                            "unit_price": 1,
                            "quote_currency": "EUR",
                            "fx_rate_to_base": 1.1,
                        },
                        self.identity_line("ASSET:CASH:USD", "cash:USD", -11),
                    ],
                )

    def test_future_fx_observation_and_non_identity_base_rate_are_rejected(self):
        with self.assertRaisesRegex(LedgerValidationError, "later than the transaction"):
            with self.db:
                self.ledger.post_transaction(
                    portfolio_id="paper-usd",
                    idempotency_key="future-fx",
                    transaction_type="FX_CONVERSION",
                    occurred_at=1_800_000_050,
                    source="TEST",
                    entries=[
                        {
                            "account_code": "ASSET:CASH:EUR",
                            "instrument_id": "cash:EUR",
                            "quantity": 10,
                            "unit_price": 1,
                            "quote_currency": "EUR",
                            "fx_rate_to_base": 1.1,
                            "fx_source": "ECB",
                            "fx_observed_at": 1_800_000_051,
                            "fx_quality": "OBSERVED",
                        },
                        self.identity_line("ASSET:CASH:USD", "cash:USD", -11),
                    ],
                )
        base_line = self.identity_line("ASSET:CASH:USD", "cash:USD", -11)
        base_line["fx_rate_to_base"] = 0.99
        with self.assertRaisesRegex(LedgerValidationError, "fx_rate_to_base=1"):
            with self.db:
                self.ledger.post_transaction(
                    portfolio_id="paper-usd",
                    idempotency_key="bad-base-rate",
                    transaction_type="ADJUSTMENT",
                    occurred_at=1_800_000_052,
                    source="TEST",
                    entries=[
                        self.identity_line("ASSET:CASH:USD", "cash:USD", 10),
                        base_line,
                    ],
                )

    def test_identical_retry_is_idempotent_and_changed_payload_conflicts(self):
        lines = [
            self.identity_line(
                "ASSET:INVENTORY:CRYPTO:BTC", "crypto:BTCUSDT", 0.01,
                unit_price=100,
            ),
            self.identity_line("ASSET:CASH:USD", "cash:USD", -1),
        ]
        kwargs = {
            "portfolio_id": "paper-usd",
            "idempotency_key": "stable-trade-id",
            "transaction_type": "TRADE",
            "occurred_at": 1_800_000_060,
            "source": "TEST_SIMULATOR",
            "entries": lines,
        }
        with self.db:
            first = self.ledger.post_transaction(**kwargs)
            repeated = self.ledger.post_transaction(**kwargs)
        self.assertTrue(first["inserted"])
        self.assertFalse(repeated["inserted"])
        self.assertEqual(first["transaction_id"], repeated["transaction_id"])
        self.assertEqual(len(self.ledger.recent_transactions("paper-usd")), 1)

        changed_lines = [dict(line) for line in lines]
        changed_lines[0]["quantity"] = 0.02
        changed_lines[1]["quantity"] = -2
        with self.assertRaises(LedgerIdempotencyConflict):
            with self.db:
                self.ledger.post_transaction(**{**kwargs, "entries": changed_lines})

    def test_account_codes_and_numeric_values_are_strictly_validated(self):
        with self.assertRaisesRegex(LedgerValidationError, "account_code"):
            with self.db:
                self.ledger.post_transaction(
                    portfolio_id="paper-usd",
                    idempotency_key="invalid-account",
                    transaction_type="ADJUSTMENT",
                    occurred_at=1_800_000_070,
                    source="TEST",
                    entries=[
                        {
                            **self.identity_line("BROKER:ASSET", "cash:USD", 1),
                        },
                        self.identity_line("ASSET:CASH:USD", "cash:USD", -1),
                    ],
                )
        with self.assertRaisesRegex(LedgerValidationError, "finite number"):
            with self.db:
                self.ledger.post_transaction(
                    portfolio_id="paper-usd",
                    idempotency_key="nan-number",
                    transaction_type="ADJUSTMENT",
                    occurred_at=1_800_000_071,
                    source="TEST",
                    entries=[
                        self.identity_line(
                            "ASSET:CASH:USD", "cash:USD", float("nan")
                        ),
                        self.identity_line(
                            "EQUITY:CAPITAL", "cash:USD", -1
                        ),
                    ],
                )

    def test_journal_rows_are_append_only(self):
        with self.db:
            self.ledger.record_capital_contribution(
                "paper-usd",
                idempotency_key="immutable-capital",
                amount=1_000,
                occurred_at=1_800_000_080,
            )
        transaction = self.ledger.recent_transactions("paper-usd")[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db:
                self.db.execute(
                    "UPDATE paper_portfolio_transactions SET source='CHANGED' "
                    "WHERE transaction_id=?",
                    (transaction["transaction_id"],),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db:
                self.db.execute(
                    "DELETE FROM paper_portfolio_entries WHERE transaction_id=?",
                    (transaction["transaction_id"],),
                )

    def test_replay_survives_database_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "portfolio-ledger.sqlite3")
            first_db = sqlite3.connect(path)
            first_db.row_factory = sqlite3.Row
            first = MultiCurrencyPaperLedger(first_db)
            with first_db:
                first.create_portfolio(
                    "persistent-paper", "USD", created_at=1_800_000_000
                )
                first.record_capital_contribution(
                    "persistent-paper",
                    idempotency_key="persistent-capital",
                    amount=2_500,
                    occurred_at=1_800_000_010,
                )
            first_db.close()

            second_db = sqlite3.connect(path)
            second_db.row_factory = sqlite3.Row
            second = MultiCurrencyPaperLedger(second_db)
            report = second.replay("persistent-paper")
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["transaction_count"], 1)
            self.assertEqual(report["entry_count"], 2)
            self.assertEqual(
                second.account_balances("persistent-paper")[0]["quantity"],
                2_500.0,
            )
            second_db.close()

    def test_replay_detects_entry_tampering(self):
        with self.db:
            self.ledger.record_capital_contribution(
                "paper-usd",
                idempotency_key="tamper-capital",
                amount=1_000,
                occurred_at=1_800_000_090,
            )
        transaction = self.ledger.recent_transactions("paper-usd")[0]
        self.db.execute("DROP TRIGGER paper_portfolio_entries_no_update")
        with self.db:
            self.db.execute(
                "UPDATE paper_portfolio_entries SET base_amount=999 "
                "WHERE transaction_id=? AND account_code='ASSET:CASH:USD'",
                (transaction["transaction_id"],),
            )
        report = self.ledger.replay("paper-usd")
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("entries do not match" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
