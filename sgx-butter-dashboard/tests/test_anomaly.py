import importlib.util
import datetime as dt
import pathlib
import sqlite3
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collector", ROOT / "sgx_butter_collector.py"
)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(collector)


class AnomalyTests(unittest.TestCase):
    def test_robust_score_flags_large_outlier(self):
        score, median = collector.robust_score(20.0, [9, 10, 10, 10, 11, 9])
        self.assertGreater(score, 4.5)
        self.assertEqual(median, 10.0)

    def test_robust_score_handles_constant_history_with_tolerance(self):
        score, median = collector.robust_score(1.0, [0, 0, 0, 0, 0], 0.5)
        self.assertTrue(score > 0)
        self.assertEqual(median, 0.0)
        score, _ = collector.robust_score(0.25, [0, 0, 0, 0, 0], 0.5)
        self.assertEqual(score, 0.0)

    def test_business_day_lag_excludes_weekend(self):
        self.assertEqual(
            collector.business_days_after("2026-06-19", dt.date(2026, 6, 21)),
            0,
        )
        self.assertEqual(
            collector.business_days_after("2026-06-19", dt.date(2026, 6, 22)),
            1,
        )

    def test_snapshot_preserves_every_field(self):
        connection = collector.connect_database(pathlib.Path(":memory:"))
        row = {
            "symbol": "BTRN26",
            "base-date": "20260619",
            "daily-settlement-price-abs": 5675,
            "custom-new-field": {"future": True},
        }
        collector.save_snapshot(
            connection, [row], "2026-06-19", "2026-06-19T12:00:00+00:00"
        )
        field = connection.execute(
            """
            SELECT field_value, value_type FROM contract_fields
            WHERE symbol='BTRN26' AND field_name='custom-new-field'
            """
        ).fetchone()
        self.assertEqual(field["value_type"], "json")
        self.assertIn('"future":true', field["field_value"])

    def test_missing_settlement_is_critical(self):
        connection = collector.connect_database(pathlib.Path(":memory:"))
        alerts = collector.detect_anomalies(
            connection,
            [{"symbol": "BTRN26", "delivery-month": "2026-07"}],
            "2026-06-19",
            collector.DEFAULT_CONFIG,
        )
        self.assertEqual(alerts[0]["rule"], "missing_settlement")
        self.assertEqual(alerts[0]["severity"], "critical")


if __name__ == "__main__":
    unittest.main()
