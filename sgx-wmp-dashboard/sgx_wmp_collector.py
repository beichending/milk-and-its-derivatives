#!/usr/bin/env python3
"""Daily SGX-NZX Global Whole Milk Powder Futures collector and anomaly detector."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import smtplib
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sgx_wmp_dashboard import generate_dashboard


API_BASE = "https://api.sgx.com/derivatives/v1.0"
PRODUCT_URL = "https://www.sgx.com/derivatives/products/dairy?cc=WMP"
DEFAULT_CONFIG: dict[str, Any] = {
    "contract_code": "WMP",
    "category": "futures",
    "history_days": "1y",
    "archive_history_days": "2y",
    "dashboard_archive_months": 24,
    "history_window": 60,
    "minimum_history": 20,
    "robust_z_threshold": 4.5,
    "price_return_abs_threshold": 0.08,
    "curve_spread_z_threshold": 4.5,
    "volume_z_threshold": 5.0,
    "open_interest_change_z_threshold": 5.0,
    "stale_business_days": 2,
    "contract_count_drop_ratio": 0.25,
    "constant_return_tolerance": 0.005,
    "constant_volume_log_tolerance": 0.69,
    "constant_open_interest_tolerance": 5.0,
    "constant_curve_spread_tolerance": 10.0,
    "request_timeout_seconds": 30,
    "request_retries": 3,
    "output_dir": "data",
    "database": "data/sgx_wmp.sqlite3",
    "alerts_file": "data/alerts.jsonl",
    "dashboard": "dashboard.html",
    "dashboard_history_days": 120,
    "target_anomaly_rate": 0.05,
    "anomaly_feature_window": 60,
    "anomaly_calibration_window": 252,
    "anomaly_minimum_calibration": 60,
    "alert": {
        "webhook_url": "",
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_starttls": True,
        "smtp_username": "",
        "smtp_password_env": "SGX_ALERT_SMTP_PASSWORD",
        "email_from": "",
        "email_to": [],
    },
}

NUMERIC_FIELDS = (
    "daily-settlement-price-abs",
    "preliminary-settlement-price-abs",
    "last-traded-price-abs",
    "best-bid-price-abs",
    "best-ask-price-abs",
    "session-open-abs",
    "session-traded-high-abs",
    "session-traded-low-abs",
    "total-volume",
    "aggregate-total-volume",
    "open-interest",
)
FUTURES_MONTH_CODES = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def market_today() -> dt.date:
    return dt.datetime.now(ZoneInfo("Asia/Singapore")).date()


def add_months(value: str, months: int) -> str:
    year, month = (int(part) for part in value.split("-", 1))
    absolute = year * 12 + month - 1 + months
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def symbol_for_delivery_month(contract_code: str, delivery_month: str) -> str:
    year, month = (int(part) for part in delivery_month.split("-", 1))
    return f"{contract_code}{FUTURES_MONTH_CODES[month]}{year % 100:02d}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return DEFAULT_CONFIG
    with path.open("r", encoding="utf-8") as handle:
        return merge_dict(DEFAULT_CONFIG, json.load(handle))


def resolve_path(config_path: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else config_path.parent / path


def fetch_json(url: str, timeout: int, retries: int) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Referer": PRODUCT_URL,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126 Safari/537.36"
        ),
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError(f"Unexpected SGX response structure from {url}")
            return payload
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"SGX request failed after {retries} attempts: {url}: {last_error}")


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            business_date TEXT,
            contract_count INTEGER,
            response_hash TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            business_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            delivery_month TEXT,
            last_trading_date TEXT,
            settlement REAL,
            preliminary_settlement REAL,
            last_price REAL,
            bid REAL,
            ask REAL,
            session_open REAL,
            session_high REAL,
            session_low REAL,
            volume REAL,
            open_interest REAL,
            record_update_time TEXT,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (business_date, symbol)
        );

        CREATE TABLE IF NOT EXISTS history (
            business_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            settlement REAL,
            last_price REAL,
            volume REAL,
            open_interest REAL,
            raw_json TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (business_date, symbol)
        );

        CREATE TABLE IF NOT EXISTS contract_fields (
            business_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            field_name TEXT NOT NULL,
            field_value TEXT,
            value_type TEXT NOT NULL,
            PRIMARY KEY (business_date, symbol, field_name)
        );

        CREATE TABLE IF NOT EXISTS alerts (
            alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            business_date TEXT,
            symbol TEXT,
            severity TEXT NOT NULL,
            rule TEXT NOT NULL,
            message TEXT NOT NULL,
            current_value REAL,
            reference_value REAL,
            score REAL,
            details_json TEXT NOT NULL,
            UNIQUE (business_date, symbol, rule, message)
        );

        CREATE INDEX IF NOT EXISTS idx_history_symbol_date
        ON history(symbol, business_date);
        CREATE INDEX IF NOT EXISTS idx_alerts_date
        ON alerts(business_date, severity);
        """
    )
    return connection


def number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def business_date_from_contracts(contracts: list[dict[str, Any]]) -> str:
    dates = [str(row.get("base-date", "")) for row in contracts if row.get("base-date")]
    if not dates:
        return utc_now().date().isoformat()
    raw = max(dates)
    return dt.datetime.strptime(raw[:8], "%Y%m%d").date().isoformat()


def normalize_history_date(row: dict[str, Any]) -> str | None:
    raw = str(row.get("base-date") or row.get("record-date") or "")
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(raw[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def field_value(value: Any) -> tuple[str | None, str]:
    if value is None:
        return None, "null"
    if isinstance(value, bool):
        return ("true" if value else "false"), "boolean"
    if isinstance(value, (int, float)):
        return str(value), "number"
    if isinstance(value, (dict, list)):
        return canonical_json(value), "json"
    return str(value), "string"


def save_snapshot(
    connection: sqlite3.Connection,
    contracts: list[dict[str, Any]],
    business_date: str,
    collected_at: str,
) -> None:
    for row in contracts:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        connection.execute(
            """
            INSERT INTO snapshots (
                business_date, symbol, collected_at, delivery_month, last_trading_date,
                settlement, preliminary_settlement, last_price, bid, ask, session_open,
                session_high, session_low, volume, open_interest, record_update_time, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(business_date, symbol) DO UPDATE SET
                collected_at=excluded.collected_at,
                delivery_month=excluded.delivery_month,
                last_trading_date=excluded.last_trading_date,
                settlement=excluded.settlement,
                preliminary_settlement=excluded.preliminary_settlement,
                last_price=excluded.last_price,
                bid=excluded.bid,
                ask=excluded.ask,
                session_open=excluded.session_open,
                session_high=excluded.session_high,
                session_low=excluded.session_low,
                volume=excluded.volume,
                open_interest=excluded.open_interest,
                record_update_time=excluded.record_update_time,
                raw_json=excluded.raw_json
            """,
            (
                business_date,
                symbol,
                collected_at,
                row.get("delivery-month"),
                row.get("last-trading-date"),
                number(row.get("daily-settlement-price-abs")),
                number(row.get("preliminary-settlement-price-abs")),
                number(row.get("last-traded-price-abs")),
                number(row.get("best-bid-price-abs")),
                number(row.get("best-ask-price-abs")),
                number(row.get("session-open-abs")),
                number(row.get("session-traded-high-abs")),
                number(row.get("session-traded-low-abs")),
                number(row.get("total-volume")),
                number(row.get("open-interest")),
                row.get("record-update-time") or row.get("last-update-time"),
                canonical_json(row),
            ),
        )
        connection.execute(
            """
            INSERT INTO history (
                business_date, symbol, settlement, last_price, volume,
                open_interest, raw_json, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'snapshot')
            ON CONFLICT(business_date, symbol) DO UPDATE SET
                settlement=excluded.settlement,
                last_price=excluded.last_price,
                volume=excluded.volume,
                open_interest=excluded.open_interest,
                raw_json=excluded.raw_json,
                source='snapshot'
            """,
            (
                business_date,
                symbol,
                number(row.get("daily-settlement-price-abs")),
                number(row.get("last-traded-price-abs")),
                number(row.get("total-volume")),
                number(row.get("open-interest")),
                canonical_json(row),
            ),
        )
        connection.execute(
            "DELETE FROM contract_fields WHERE business_date=? AND symbol=?",
            (business_date, symbol),
        )
        for name, value in row.items():
            serialized, value_type = field_value(value)
            connection.execute(
                """
                INSERT INTO contract_fields
                (business_date, symbol, field_name, field_value, value_type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (business_date, symbol, str(name), serialized, value_type),
            )


def save_history(
    connection: sqlite3.Connection, symbol: str, rows: list[dict[str, Any]]
) -> int:
    saved = 0
    for row in rows:
        business_date = normalize_history_date(row)
        if not business_date:
            continue
        connection.execute(
            """
            INSERT INTO history (
                business_date, symbol, settlement, last_price, volume,
                open_interest, raw_json, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'sgx-history')
            ON CONFLICT(business_date, symbol) DO NOTHING
            """,
            (
                business_date,
                symbol,
                number(row.get("daily-settlement-price-abs")),
                number(row.get("last-traded-price-abs")),
                number(row.get("total-volume")),
                number(row.get("open-interest")),
                canonical_json(row),
            ),
        )
        saved += connection.execute("SELECT changes()").fetchone()[0]
    return saved


def robust_score(
    value: float,
    values: Iterable[float],
    constant_tolerance: float = 0.0,
) -> tuple[float | None, float | None]:
    sample = [float(item) for item in values if item is not None and math.isfinite(float(item))]
    if len(sample) < 5:
        return None, None
    median = statistics.median(sample)
    mad = statistics.median(abs(item - median) for item in sample)
    if mad == 0:
        difference = value - median
        if abs(difference) <= constant_tolerance:
            return 0.0, median
        return math.copysign(math.inf, difference), median
    return 0.67448975 * (value - median) / mad, median


def severity_for_score(score: float | None, threshold: float) -> str:
    if score is not None and abs(score) >= threshold * 1.5:
        return "critical"
    return "warning"


def make_alert(
    business_date: str,
    symbol: str | None,
    severity: str,
    rule: str,
    message: str,
    current_value: float | None = None,
    reference_value: float | None = None,
    score: float | None = None,
    **details: Any,
) -> dict[str, Any]:
    return {
        "created_at": utc_now().isoformat(),
        "business_date": business_date,
        "symbol": symbol,
        "severity": severity,
        "rule": rule,
        "message": message,
        "current_value": current_value,
        "reference_value": reference_value,
        "score": score,
        "details": details,
    }


def historical_rows(
    connection: sqlite3.Connection, symbol: str, before_date: str, limit: int
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT business_date, settlement, last_price, volume, open_interest
        FROM history
        WHERE symbol=? AND business_date < ?
        ORDER BY business_date DESC
        LIMIT ?
        """,
        (symbol, before_date, limit + 1),
    ).fetchall()


def business_days_after(value: str, today: dt.date | None = None) -> int:
    start = dt.date.fromisoformat(value)
    end = today or market_today()
    if start >= end:
        return 0
    count = 0
    cursor = start + dt.timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            count += 1
        cursor += dt.timedelta(days=1)
    return count


def detect_anomalies(
    connection: sqlite3.Connection,
    contracts: list[dict[str, Any]],
    business_date: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    window = int(config["history_window"])
    minimum = int(config["minimum_history"])
    z_threshold = float(config["robust_z_threshold"])

    lag = business_days_after(business_date)
    stale_limit = int(config["stale_business_days"])
    if lag > stale_limit:
        alerts.append(
            make_alert(
                business_date,
                None,
                "critical",
                "stale_market_data",
                f"SGX WMP data is {lag} business days old",
                current_value=float(lag),
                reference_value=float(stale_limit),
            )
        )

    symbols = [str(row.get("symbol") or "").strip() for row in contracts]
    duplicate_symbols = sorted(
        symbol for symbol in set(symbols) if symbol and symbols.count(symbol) > 1
    )
    for symbol in duplicate_symbols:
        alerts.append(
            make_alert(
                business_date,
                symbol,
                "critical",
                "duplicate_contract",
                f"SGX returned duplicate rows for contract {symbol}",
            )
        )

    previous_counts = [
        row[0]
        for row in connection.execute(
            """
            SELECT contract_count FROM runs
            WHERE status='success' AND business_date < ?
              AND contract_count IS NOT NULL
            ORDER BY business_date DESC LIMIT 20
            """,
            (business_date,),
        )
    ]
    if len(previous_counts) >= 5:
        reference_count = statistics.median(previous_counts)
        drop_ratio = float(config["contract_count_drop_ratio"])
        if len(contracts) < reference_count * (1 - drop_ratio):
            alerts.append(
                make_alert(
                    business_date,
                    None,
                    "critical",
                    "contract_count_drop",
                    (
                        f"SGX returned {len(contracts)} WMP contracts; "
                        f"recent median is {reference_count:g}"
                    ),
                    current_value=float(len(contracts)),
                    reference_value=float(reference_count),
                )
            )

    previous_snapshot_date_row = connection.execute(
        "SELECT MAX(business_date) FROM snapshots WHERE business_date < ?",
        (business_date,),
    ).fetchone()
    previous_snapshot_date = previous_snapshot_date_row[0]
    if previous_snapshot_date:
        previous_fields = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT field_name FROM contract_fields WHERE business_date=?",
                (previous_snapshot_date,),
            )
        }
        current_fields = {str(key) for row in contracts for key in row}
        for field in sorted(previous_fields - current_fields):
            alerts.append(
                make_alert(
                    business_date,
                    None,
                    "warning",
                    "schema_field_removed",
                    f"SGX WMP response no longer contains field: {field}",
                    previous_business_date=previous_snapshot_date,
                )
            )
        for field in sorted(current_fields - previous_fields):
            alerts.append(
                make_alert(
                    business_date,
                    None,
                    "info",
                    "schema_field_added",
                    f"SGX WMP response contains a new field: {field}",
                    previous_business_date=previous_snapshot_date,
                )
            )

    previous_symbols = {
        row[0]
        for row in connection.execute(
            """
            SELECT symbol FROM snapshots
            WHERE business_date = (
                SELECT MAX(business_date) FROM snapshots WHERE business_date < ?
            )
            """,
            (business_date,),
        )
    }
    current_symbols = {str(row.get("symbol")) for row in contracts if row.get("symbol")}
    for symbol in sorted(current_symbols - previous_symbols) if previous_symbols else []:
        alerts.append(
            make_alert(
                business_date, symbol, "info", "new_contract",
                f"New listed/observed contract: {symbol}",
            )
        )
    for symbol in sorted(previous_symbols - current_symbols):
        alerts.append(
            make_alert(
                business_date, symbol, "info", "contract_removed",
                f"Contract no longer returned by SGX: {symbol}",
            )
        )

    curve_rows: list[tuple[str, str, float]] = []
    for row in contracts:
        symbol = str(row.get("symbol") or "")
        settlement = number(row.get("daily-settlement-price-abs"))
        preliminary = number(row.get("preliminary-settlement-price-abs"))
        volume = number(row.get("total-volume"))
        oi = number(row.get("open-interest"))
        history = historical_rows(connection, symbol, business_date, window)

        if settlement is None or settlement <= 0:
            alerts.append(
                make_alert(
                    business_date, symbol, "critical", "missing_settlement",
                    f"{symbol}: missing or non-positive daily settlement price",
                    current_value=settlement,
                )
            )
            continue

        if row.get("delivery-month"):
            curve_rows.append((str(row["delivery-month"]), symbol, settlement))

        if preliminary is not None and settlement:
            gap = (preliminary - settlement) / settlement
            if abs(gap) >= 0.01:
                alerts.append(
                    make_alert(
                        business_date, symbol, "warning", "preliminary_settlement_gap",
                        f"{symbol}: preliminary and final settlement differ by {gap:.2%}",
                        current_value=preliminary,
                        reference_value=settlement,
                        score=gap,
                    )
                )

        if len(history) < minimum:
            continue

        settlements = [item["settlement"] for item in history if item["settlement"]]
        if settlements:
            previous = settlements[0]
            daily_return = settlement / previous - 1
            past_returns = [
                settlements[index] / settlements[index + 1] - 1
                for index in range(len(settlements) - 1)
                if settlements[index + 1]
            ]
            score, median = robust_score(
                daily_return,
                past_returns,
                float(config["constant_return_tolerance"]),
            )
            hard_limit = float(config["price_return_abs_threshold"])
            if abs(daily_return) >= hard_limit or (
                score is not None and abs(score) >= z_threshold
            ):
                alerts.append(
                    make_alert(
                        business_date,
                        symbol,
                        severity_for_score(score, z_threshold),
                        "settlement_return",
                        f"{symbol}: settlement moved {daily_return:.2%} vs previous day",
                        current_value=daily_return,
                        reference_value=median,
                        score=score,
                        previous_settlement=previous,
                        settlement=settlement,
                    )
                )

        if volume is not None:
            past_volumes = [item["volume"] for item in history if item["volume"] is not None]
            score, median = robust_score(
                math.log1p(volume),
                [math.log1p(x) for x in past_volumes],
                float(config["constant_volume_log_tolerance"]),
            )
            volume_threshold = float(config["volume_z_threshold"])
            if score is not None and score >= volume_threshold:
                alerts.append(
                    make_alert(
                        business_date, symbol, severity_for_score(score, volume_threshold),
                        "volume_spike", f"{symbol}: unusual volume {volume:g}",
                        current_value=volume, reference_value=math.expm1(median) if median is not None else None,
                        score=score,
                    )
                )

        if oi is not None:
            oi_series = [item["open_interest"] for item in history if item["open_interest"] is not None]
            if oi_series:
                oi_change = oi - oi_series[0]
                past_changes = [
                    oi_series[index] - oi_series[index + 1]
                    for index in range(len(oi_series) - 1)
                ]
                score, median = robust_score(
                    oi_change,
                    past_changes,
                    float(config["constant_open_interest_tolerance"]),
                )
                oi_threshold = float(config["open_interest_change_z_threshold"])
                if score is not None and abs(score) >= oi_threshold:
                    alerts.append(
                        make_alert(
                            business_date, symbol, severity_for_score(score, oi_threshold),
                            "open_interest_change",
                            f"{symbol}: unusual open-interest change {oi_change:+g}",
                            current_value=oi_change, reference_value=median, score=score,
                        )
                    )

    curve_rows.sort()
    current_spreads: list[tuple[str, str, float]] = []
    for left, right in zip(curve_rows, curve_rows[1:]):
        current_spreads.append((left[1], right[1], right[2] - left[2]))
    spread_threshold = float(config["curve_spread_z_threshold"])
    for front, back, spread in current_spreads:
        past = connection.execute(
            """
            SELECT a.settlement AS front_price, b.settlement AS back_price
            FROM history a JOIN history b ON a.business_date=b.business_date
            WHERE a.symbol=? AND b.symbol=? AND a.business_date < ?
              AND a.settlement IS NOT NULL AND b.settlement IS NOT NULL
            ORDER BY a.business_date DESC LIMIT ?
            """,
            (front, back, business_date, window),
        ).fetchall()
        if len(past) < minimum:
            continue
        score, median = robust_score(
            spread,
            [row["back_price"] - row["front_price"] for row in past],
            float(config["constant_curve_spread_tolerance"]),
        )
        if score is not None and abs(score) >= spread_threshold:
            alerts.append(
                make_alert(
                    business_date, f"{front}/{back}",
                    severity_for_score(score, spread_threshold), "curve_spread",
                    f"{front}/{back}: unusual adjacent-month spread {spread:+g}",
                    current_value=spread, reference_value=median, score=score,
                )
            )
    return alerts


def save_alerts(
    connection: sqlite3.Connection, alerts: list[dict[str, Any]], path: Path
) -> list[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    inserted: list[dict[str, Any]] = []
    for alert in alerts:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO alerts (
                created_at, business_date, symbol, severity, rule, message,
                current_value, reference_value, score, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert["created_at"],
                alert["business_date"],
                alert["symbol"],
                alert["severity"],
                alert["rule"],
                alert["message"],
                alert["current_value"],
                alert["reference_value"],
                alert["score"],
                canonical_json(alert["details"]),
            ),
        )
        if cursor.rowcount:
            inserted.append(alert)
    if inserted:
        with path.open("a", encoding="utf-8") as handle:
            for alert in inserted:
                handle.write(canonical_json(alert) + "\n")
    return inserted


def export_snapshot(contracts: list[dict[str, Any]], business_date: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in contracts for key in row})
    preferred = ["symbol", "contract-code", "contract-name", "delivery-month", "base-date"]
    fields = [key for key in preferred if key in fields] + [
        key for key in fields if key not in preferred
    ]
    path = output_dir / f"sgx_wmp_{business_date}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(contracts)
    return path


def alert_text(alerts: list[dict[str, Any]]) -> str:
    lines = [f"SGX Whole Milk Powder Futures alerts: {len(alerts)}"]
    lines.extend(
        f"[{item['severity'].upper()}] {item['message']}" for item in alerts
    )
    return "\n".join(lines)


def write_run_log(config_path: Path, text: str) -> None:
    log_dir = config_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"collector-{market_today().isoformat()}.log"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now().isoformat()} {text}\n")


def send_notifications(alerts: list[dict[str, Any]], config: dict[str, Any]) -> None:
    actionable = [item for item in alerts if item["severity"] in {"warning", "critical"}]
    if not actionable:
        return
    settings = config.get("alert", {})
    text = alert_text(actionable)
    webhook_url = str(settings.get("webhook_url") or "").strip()
    if webhook_url:
        payload = canonical_json({"text": text, "alerts": actionable}).encode("utf-8")
        request = urllib.request.Request(
            webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(
            request, timeout=int(config["request_timeout_seconds"])
        ):
            pass

    recipients = settings.get("email_to") or []
    if settings.get("smtp_host") and recipients:
        message = EmailMessage()
        message["Subject"] = f"SGX Whole Milk Powder Futures alert ({len(actionable)})"
        message["From"] = settings.get("email_from") or settings.get("smtp_username")
        message["To"] = ", ".join(recipients)
        message.set_content(text)
        password = os.environ.get(str(settings.get("smtp_password_env") or ""), "")
        with smtplib.SMTP(
            settings["smtp_host"], int(settings.get("smtp_port", 587)), timeout=30
        ) as smtp:
            if settings.get("smtp_starttls", True):
                smtp.starttls()
            if settings.get("smtp_username"):
                smtp.login(settings["smtp_username"], password)
            smtp.send_message(message)


def bootstrap_history(
    connection: sqlite3.Connection,
    contracts: list[dict[str, Any]],
    config: dict[str, Any],
) -> int:
    total = 0
    timeout = int(config["request_timeout_seconds"])
    retries = int(config["request_retries"])
    for row in contracts:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        count = connection.execute(
            "SELECT COUNT(*) FROM history WHERE symbol=?", (symbol,)
        ).fetchone()[0]
        if count >= int(config["minimum_history"]):
            continue
        url = (
            f"{API_BASE}/history/symbol/{symbol}?category={config['category']}"
            f"&days={config['history_days']}"
        )
        payload = fetch_json(url, timeout, retries)
        total += save_history(connection, symbol, payload["data"])
        connection.commit()
        time.sleep(0.1)

    delivery_months = sorted(
        str(row["delivery-month"]) for row in contracts if row.get("delivery-month")
    )
    if delivery_months:
        archive_months = int(config.get("dashboard_archive_months", 0))
        front_month = delivery_months[0]
        current_symbols = {
            str(row.get("symbol") or "") for row in contracts if row.get("symbol")
        }
        for offset in range(archive_months, 0, -1):
            delivery_month = add_months(front_month, -offset)
            symbol = symbol_for_delivery_month(
                str(config["contract_code"]), delivery_month
            )
            if symbol in current_symbols:
                continue
            count = connection.execute(
                "SELECT COUNT(*) FROM history WHERE symbol=?", (symbol,)
            ).fetchone()[0]
            if count >= int(config["minimum_history"]):
                continue
            url = (
                f"{API_BASE}/history/symbol/{symbol}?category={config['category']}"
                f"&days={config['archive_history_days']}"
            )
            payload = fetch_json(url, timeout, retries)
            total += save_history(connection, symbol, payload["data"])
            connection.commit()
            time.sleep(0.1)
    return total


def collect(config_path: Path, bootstrap: bool = True, notify: bool = True) -> int:
    config = load_config(config_path)
    output_dir = resolve_path(config_path, config["output_dir"])
    database_path = resolve_path(config_path, config["database"])
    alerts_path = resolve_path(config_path, config["alerts_file"])
    connection = connect_database(database_path)
    started_at = utc_now().isoformat()
    run_id = connection.execute(
        "INSERT INTO runs(started_at, status) VALUES (?, 'running')", (started_at,)
    ).lastrowid
    connection.commit()
    try:
        url = (
            f"{API_BASE}/contract-code/{config['contract_code']}"
            f"?category={config['category']}"
        )
        payload = fetch_json(
            url, int(config["request_timeout_seconds"]), int(config["request_retries"])
        )
        contracts = payload["data"]
        if not contracts:
            raise RuntimeError("SGX returned no WMP contracts")
        business_date = business_date_from_contracts(contracts)
        response_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        if bootstrap:
            bootstrap_history(connection, contracts, config)
        alerts = detect_anomalies(connection, contracts, business_date, config)
        save_snapshot(connection, contracts, business_date, utc_now().isoformat())
        inserted_alerts = save_alerts(connection, alerts, alerts_path)
        csv_path = export_snapshot(contracts, business_date, output_dir)
        raw_path = output_dir / f"sgx_wmp_{business_date}.json"
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        dashboard_path = generate_dashboard(
            connection,
            resolve_path(config_path, config["dashboard"]),
            business_date=business_date,
            days=int(config["dashboard_history_days"]),
            target_anomaly_rate=float(config["target_anomaly_rate"]),
            anomaly_feature_window=int(config["anomaly_feature_window"]),
            anomaly_calibration_window=int(config["anomaly_calibration_window"]),
            anomaly_minimum_calibration=int(config["anomaly_minimum_calibration"]),
        )
        connection.execute(
            """
            UPDATE runs SET completed_at=?, status='success', business_date=?,
                contract_count=?, response_hash=? WHERE run_id=?
            """,
            (utc_now().isoformat(), business_date, len(contracts), response_hash, run_id),
        )
        connection.commit()
        if notify:
            send_notifications(inserted_alerts, config)
        result = (
            f"OK business_date={business_date} contracts={len(contracts)} "
            f"new_alerts={len(inserted_alerts)} csv={csv_path} dashboard={dashboard_path}"
        )
        print(result)
        write_run_log(config_path, result)
        return 0
    except Exception as exc:
        connection.execute(
            "UPDATE runs SET completed_at=?, status='failed', error=? WHERE run_id=?",
            (utc_now().isoformat(), str(exc), run_id),
        )
        connection.commit()
        result = f"ERROR {exc}"
        print(result, file=sys.stderr)
        write_run_log(config_path, result)
        return 1
    finally:
        connection.close()


def status(config_path: Path) -> int:
    config = load_config(config_path)
    database_path = resolve_path(config_path, config["database"])
    if not database_path.exists():
        print("No database yet. Run collect first.")
        return 1
    connection = connect_database(database_path)
    last_run = connection.execute(
        "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    counts = connection.execute(
        """
        SELECT COUNT(DISTINCT business_date) AS days,
               COUNT(DISTINCT symbol) AS symbols,
               COUNT(*) AS rows
        FROM history
        """
    ).fetchone()
    open_alerts = connection.execute(
        "SELECT severity, COUNT(*) AS count FROM alerts GROUP BY severity ORDER BY severity"
    ).fetchall()
    print(f"database={database_path}")
    print(f"history_days={counts['days']} symbols={counts['symbols']} rows={counts['rows']}")
    if last_run:
        print(
            f"last_run={last_run['status']} business_date={last_run['business_date']} "
            f"contracts={last_run['contract_count']} completed_at={last_run['completed_at']}"
        )
    print("alerts=" + ", ".join(f"{row['severity']}:{row['count']}" for row in open_alerts))
    connection.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default="config.json", help="Path to JSON configuration file"
    )
    subparsers = parser.add_subparsers(dest="command")
    collect_parser = subparsers.add_parser("collect", help="Collect, store, export, and alert")
    collect_parser.add_argument("--no-bootstrap", action="store_true")
    collect_parser.add_argument("--no-notify", action="store_true")
    subparsers.add_parser("status", help="Show database and latest-run status")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    if args.command in (None, "collect"):
        return collect(
            config_path,
            bootstrap=not getattr(args, "no_bootstrap", False),
            notify=not getattr(args, "no_notify", False),
        )
    if args.command == "status":
        return status(config_path)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
