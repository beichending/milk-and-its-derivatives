import importlib.util
import datetime as dt
import json
import pathlib
import sqlite3
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dashboard", ROOT / "sgx_butter_dashboard.py"
)
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(dashboard)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE snapshots (
                business_date TEXT, symbol TEXT, delivery_month TEXT,
                last_trading_date TEXT, settlement REAL,
                preliminary_settlement REAL, last_price REAL, bid REAL, ask REAL,
                volume REAL, open_interest REAL, record_update_time TEXT
            );
            CREATE TABLE history (
                business_date TEXT, symbol TEXT, settlement REAL,
                volume REAL, open_interest REAL, raw_json TEXT
            );
            CREATE TABLE alerts (
                alert_id INTEGER PRIMARY KEY, created_at TEXT, business_date TEXT,
                symbol TEXT, severity TEXT, rule TEXT, message TEXT,
                current_value REAL, reference_value REAL, score REAL
            );
            """
        )
        symbols = [
            ("BTRN26", "2026-07"),
            ("BTRQ26", "2026-08"),
            ("BTRU26", "2026-09"),
            ("BTRV26", "2026-10"),
            ("BTRX26", "2026-11"),
            ("BTRZ26", "2026-12"),
            ("BTRF27", "2027-01"),
        ]
        for index, (symbol, delivery_month) in enumerate(symbols, start=1):
            self.connection.execute(
                """
                INSERT INTO snapshots VALUES
                ('2026-06-19', ?, ?, '2027-01-01', ?, ?, NULL, NULL, NULL, ?, ?, '')
                """,
                (symbol, delivery_month, 5000 + index, 5000 + index, index, 100),
            )
            for day in range(1, 22):
                self.connection.execute(
                    """
                    INSERT INTO history
                    (business_date, symbol, settlement, volume, open_interest)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        f"2026-05-{day:02d}",
                        symbol,
                        4900 + day + index,
                        day % 3,
                        90 + day,
                    ),
                )
            self.connection.execute(
                """
                INSERT INTO history
                (business_date, symbol, settlement, volume, open_interest, raw_json)
                VALUES ('2026-06-19', ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    5000 + index,
                    index,
                    100,
                    json.dumps(
                        {
                            "best-bid-price-abs": 4995,
                            "best-ask-price-abs": 5005,
                        }
                        if symbol == "BTRN26"
                        else {}
                    ),
                ),
            )
        self.connection.execute(
            """
            INSERT INTO history
            (business_date, symbol, settlement, volume, open_interest)
            VALUES ('2026-05-20', 'BTRM26', 4990, 2, 90)
            """
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def test_payload_uses_nearest_six_contracts(self):
        payload = dashboard.build_payload(
            self.connection, business_date="2026-06-19", days=30
        )
        self.assertEqual(len(payload["contracts"]), 6)
        self.assertEqual(payload["contracts"][0]["symbol"], "BTRN26")
        self.assertEqual(payload["contracts"][-1]["symbol"], "BTRZ26")
        self.assertEqual(payload["distant_contract"]["symbol"], "BTRF27")
        self.assertEqual(payload["summary"]["distant_delivery_month"], "2027-01")
        self.assertEqual(payload["summary"]["current_spread"], 6)
        self.assertEqual(payload["spread_series"][-1]["spread"], 6)
        self.assertEqual(payload["contracts"][0]["bid"], 4995)
        self.assertEqual(payload["contracts"][0]["ask"], 5005)
        self.assertEqual(payload["contracts"][0]["bid_ask_gap"], 10)
        self.assertEqual(payload["summary"]["two_sided_quote_count"], 1)
        self.assertIn("2026-06-19", payload["available_dates"])
        self.assertEqual(
            payload["views"]["2026-06-19"]["summary"]["front_symbol"], "BTRN26"
        )
        self.assertIn("Best estimate", payload["estimate"]["headline"])

    def test_adds_six_calendar_months(self):
        self.assertEqual(dashboard.add_months("2026-07", 6), "2027-01")
        self.assertEqual(dashboard.add_months("2027-10", 6), "2028-04")

    def test_historical_date_rolls_front_and_distant_contracts(self):
        self.connection.execute(
            """
            INSERT INTO history
            (business_date, symbol, settlement, volume, open_interest)
            VALUES ('2025-11-20', 'BTRZ25', 4790, 1, 60)
            """
        )
        for index, symbol in enumerate(
            ["BTRF26", "BTRG26", "BTRH26", "BTRJ26", "BTRK26", "BTRM26"],
            start=1,
        ):
            self.connection.execute(
                """
                INSERT INTO history
                (business_date, symbol, settlement, volume, open_interest)
                VALUES ('2025-12-19', ?, ?, ?, ?)
                """,
                (symbol, 4800 + index, index, 70 + index),
            )
        self.connection.execute(
            """
            INSERT INTO history
            (business_date, symbol, settlement, volume, open_interest)
            VALUES ('2025-12-19', 'BTRN26', 4810, 7, 80)
            """
        )
        self.connection.commit()
        payload = dashboard.build_payload(
            self.connection, business_date="2025-12-19", days=30
        )
        self.assertEqual(payload["summary"]["front_symbol"], "BTRF26")
        self.assertEqual(payload["summary"]["distant_symbol"], "BTRN26")

    def test_incomplete_requested_date_falls_back_to_latest_complete_date(self):
        self.connection.execute(
            """
            INSERT INTO history
            (business_date, symbol, settlement, volume, open_interest)
            VALUES ('2026-06-22', 'BTRN26', NULL, 0, 100)
            """
        )
        self.connection.commit()
        payload = dashboard.build_payload(
            self.connection, business_date="2026-06-22", days=30
        )
        self.assertEqual(payload["meta"]["business_date"], "2026-06-19")

    def test_generates_self_contained_html(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "dashboard.html"
            dashboard.generate_dashboard(
                self.connection, path, business_date="2026-06-19", days=30
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("SGX Butter Futures Dashboard", content)
            self.assertIn('"front_symbol":"BTRN26"', content)
            self.assertIn('"distant_symbol":"BTRF27"', content)
            self.assertIn("spreadChart", content)
            self.assertIn("asOfDate", content)
            self.assertIn("最近六个合约 · Bid / Ask / Gap", content)
            self.assertIn("暂无有效双边报价", content)
            self.assertIn("业务含义", content)
            self.assertIn("观察重点", content)
            self.assertNotIn("__DATA__", content)

    def test_non_positive_price_is_treated_as_missing(self):
        self.connection.execute(
            """
            INSERT INTO history
            (business_date, symbol, settlement, volume, open_interest)
            VALUES ('2026-06-18', 'BTRN26', 0, 8, 100)
            """
        )
        self.connection.commit()
        payload = dashboard.build_payload(
            self.connection, business_date="2026-06-19", days=30
        )
        zero_point = next(
            point
            for point in payload["series"][0]["points"]
            if point["date"] == "2026-06-18"
        )
        self.assertIsNone(zero_point["settlement"])
        self.assertEqual(zero_point["volume"], 8)

    def test_calibrated_anomaly_rate_is_close_to_target(self):
        views = {}
        start = dt.date(2025, 1, 1)
        for index in range(320):
            business_date = (start + dt.timedelta(days=index)).isoformat()
            spike = index > 0 and index % 20 == 0
            contracts = [
                {
                    "daily_change": 0.05 if spike and contract_index == 0 else 0.001,
                }
                for contract_index in range(6)
            ]
            views[business_date] = {
                "summary": {
                    "total_volume": 500 if spike else 10 + index % 5,
                    "total_open_interest": 1000 + index + (100 if spike else 0),
                    "current_spread": -100 + index % 7 + (80 if spike else 0),
                },
                "contracts": contracts,
                "alerts": [],
            }
        stats = dashboard.calibrate_anomaly_days(
            views,
            target_rate=0.05,
            feature_window=60,
            calibration_window=252,
            minimum_calibration=60,
        )
        self.assertGreater(stats["eligible_days"], 200)
        self.assertGreater(stats["actual_rate"], 0.02)
        self.assertLess(stats["actual_rate"], 0.08)
        self.assertEqual(
            sum(
                1
                for view in views.values()
                if any(
                    alert["rule"] == "calibrated_daily_anomaly"
                    for alert in view["alerts"]
                )
            ),
            stats["alert_days"],
        )


if __name__ == "__main__":
    unittest.main()
