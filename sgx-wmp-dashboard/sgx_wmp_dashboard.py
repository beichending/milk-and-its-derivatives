#!/usr/bin/env python3
"""Generate a self-contained SGX Whole Milk Powder Futures business dashboard."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import sqlite3
import statistics
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


COLORS = ["#175CD3", "#0E9384", "#F79009", "#7A5AF8", "#D92D20", "#667085"]
DATA_QUALITY_ALERT_RULES = {
    "stale_market_data",
    "duplicate_contract",
    "contract_count_drop",
    "schema_field_removed",
}
MONTH_CODE_TO_NUMBER = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}


def number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return current / previous - 1


def positive_price(value: Any) -> float | None:
    result = number(value)
    return result if result is not None and result > 0 else None


def market_today() -> dt.date:
    return dt.datetime.now(ZoneInfo("Asia/Singapore")).date()


def business_day_lag(value: str) -> int:
    start = dt.date.fromisoformat(value)
    end = market_today()
    cursor = start + dt.timedelta(days=1)
    count = 0
    while cursor <= end:
        if cursor.weekday() < 5:
            count += 1
        cursor += dt.timedelta(days=1)
    return max(count, 0)


def add_months(value: str, months: int) -> str:
    year, month = (int(part) for part in value.split("-", 1))
    absolute = year * 12 + month - 1 + months
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def month_distance(left: str, right: str) -> int:
    left_year, left_month = (int(part) for part in left.split("-", 1))
    right_year, right_month = (int(part) for part in right.split("-", 1))
    return (left_year - right_year) * 12 + left_month - right_month


def delivery_month_from_symbol(symbol: str) -> str | None:
    if len(symbol) < 6 or not symbol.startswith("WMP"):
        return None
    month = MONTH_CODE_TO_NUMBER.get(symbol[-3])
    try:
        year = 2000 + int(symbol[-2:])
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}" if month else None


def contract_series(
    connection: sqlite3.Connection, symbol: str, business_date: str, limit: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT business_date, settlement, volume, open_interest
        FROM history
        WHERE symbol=? AND business_date<=?
        ORDER BY business_date DESC LIMIT ?
        """,
        (symbol, business_date, limit),
    ).fetchall()
    return [
        {
            "date": row["business_date"],
            "settlement": positive_price(row["settlement"]),
            "volume": number(row["volume"]) or 0,
            "open_interest": number(row["open_interest"]),
        }
        for row in reversed(rows)
    ]


def average_return(series: list[dict[str, Any]], lookback: int) -> float | None:
    prices = [point["settlement"] for point in series if point["settlement"] is not None]
    if len(prices) <= lookback or prices[-lookback - 1] == 0:
        return None
    return prices[-1] / prices[-lookback - 1] - 1


def make_estimate(
    contracts: list[dict[str, Any]],
    series: list[dict[str, Any]],
    distant_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    front_series = series[:3]
    returns_5 = [
        value
        for value in (average_return(item["points"], 5) for item in front_series)
        if value is not None
    ]
    returns_20 = [
        value
        for value in (average_return(item["points"], 20) for item in front_series)
        if value is not None
    ]
    momentum_5 = statistics.mean(returns_5) if returns_5 else None
    momentum_20 = statistics.mean(returns_20) if returns_20 else None

    def clipped(value: float | None, scale: float) -> float:
        if value is None:
            return 0.0
        return max(-1.0, min(1.0, value / scale))

    score = 0.65 * clipped(momentum_5, 0.02) + 0.35 * clipped(momentum_20, 0.04)
    if score >= 0.25:
        direction = "偏强"
        direction_class = "positive"
        action = "价格重心温和上移"
    elif score <= -0.25:
        direction = "偏弱"
        direction_class = "negative"
        action = "价格重心温和下移"
    else:
        direction = "区间震荡"
        direction_class = "neutral"
        action = "延续区间整理"

    front_price = contracts[0]["settlement"] if contracts else None
    back_price = (
        distant_contract["settlement"]
        if distant_contract is not None
        else contracts[-1]["settlement"] if contracts else None
    )
    curve_change = pct_change(back_price, front_price)
    if curve_change is None:
        curve_text = "期限结构信号不足"
    elif curve_change <= -0.01:
        curve_text = f"近月较 6 个月远月溢价 {abs(curve_change):.1%}，现货端偏紧"
    elif curve_change >= 0.01:
        curve_text = f"6 个月远月较近月升水 {curve_change:.1%}，曲线呈正向结构"
    else:
        curve_text = "前六个合约期限结构较平"

    volume_points = [
        point["volume"]
        for item in series
        for point in item["points"][-20:]
        if point["volume"] is not None
    ]
    nonzero_ratio = (
        sum(1 for value in volume_points if value > 0) / len(volume_points)
        if volume_points
        else 0
    )
    if len(returns_5) >= 2 and nonzero_ratio >= 0.35:
        confidence = "中"
    else:
        confidence = "低"

    time_horizon = "未来 1–2 周"
    headline = f"Best estimate：{time_horizon}最可能{action}"
    if curve_change is not None and curve_change <= -0.01:
        headline += "，近月仍相对坚挺"
    elif curve_change is not None and curve_change >= 0.01:
        headline += "，远月相对更强"
    headline += "。"

    rationale = [
        (
            f"近月前三合约 5 日平均变动为 {momentum_5:+.2%}"
            if momentum_5 is not None
            else "近月 5 日动量数据不足"
        ),
        (
            f"20 日平均变动为 {momentum_20:+.2%}"
            if momentum_20 is not None
            else "20 日动量数据不足"
        ),
        curve_text,
        (
            "成交不连续，价格信号的确认度有限"
            if nonzero_ratio < 0.35
            else "成交活跃度足以提供一定价格确认"
        ),
    ]
    return {
        "headline": headline,
        "direction": direction,
        "direction_class": direction_class,
        "confidence": confidence,
        "score": score,
        "momentum_5": momentum_5,
        "momentum_20": momentum_20,
        "curve_change": curve_change,
        "rationale": rationale,
        "disclaimer": "模型判断基于历史结算价、期限结构与成交活跃度，不构成交易建议。",
    }


def load_history_universe(
    connection: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    history_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(history)")
    }
    raw_expression = "raw_json" if "raw_json" in history_columns else "NULL AS raw_json"
    rows = connection.execute(
        f"""
        SELECT business_date, symbol, settlement, volume, open_interest,
               {raw_expression}
        FROM history ORDER BY symbol, business_date
        """
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        delivery_month = delivery_month_from_symbol(row["symbol"])
        if not delivery_month:
            continue
        raw: dict[str, Any] = {}
        if row["raw_json"]:
            try:
                parsed = json.loads(row["raw_json"])
                raw = parsed if isinstance(parsed, dict) else {}
            except (TypeError, json.JSONDecodeError):
                raw = {}
        result.setdefault(row["symbol"], []).append(
            {
                "date": row["business_date"],
                "settlement": positive_price(row["settlement"]),
                "volume": number(row["volume"]) or 0,
                "open_interest": number(row["open_interest"]),
                "bid": positive_price(raw.get("best-bid-price-abs")),
                "ask": positive_price(raw.get("best-ask-price-abs")),
            }
        )
    return result


def load_latest_snapshot_quotes(
    connection: sqlite3.Connection, requested_date: str | None = None
) -> tuple[str | None, dict[str, dict[str, float | None]]]:
    table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='snapshots'
        """
    ).fetchone()
    if not table_exists:
        return None, {}
    if requested_date:
        date_row = connection.execute(
            "SELECT MAX(business_date) FROM snapshots WHERE business_date <= ?",
            (requested_date,),
        ).fetchone()
    else:
        date_row = connection.execute(
            "SELECT MAX(business_date) FROM snapshots"
        ).fetchone()
    quote_date = date_row[0] if date_row else None
    if not quote_date:
        return None, {}
    rows = connection.execute(
        """
        SELECT symbol, bid, ask
        FROM snapshots WHERE business_date=?
        """,
        (quote_date,),
    ).fetchall()
    return quote_date, {
        row["symbol"]: {
            "bid": positive_price(row["bid"]),
            "ask": positive_price(row["ask"]),
        }
        for row in rows
    }


def apply_snapshot_quotes(
    view: dict[str, Any],
    quote_date: str | None,
    quotes: dict[str, dict[str, float | None]],
) -> None:
    if not quote_date or not quotes:
        return
    for contract in view["contracts"]:
        quote = quotes.get(contract["symbol"])
        if not quote:
            continue
        contract["bid"] = quote["bid"]
        contract["ask"] = quote["ask"]
        contract["bid_ask_gap"] = (
            quote["ask"] - quote["bid"]
            if quote["bid"] is not None and quote["ask"] is not None
            else None
        )
    view["summary"]["two_sided_quote_count"] = sum(
        1 for item in view["contracts"] if item["bid_ask_gap"] is not None
    )
    view["summary"]["quote_date"] = quote_date


def build_historical_view(
    history: dict[str, list[dict[str, Any]]],
    selected_date: str,
    days: int,
    alerts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    selected_month = selected_date[:7]
    candidates: list[tuple[str, str, list[dict[str, Any]], dict[str, Any]]] = []
    for symbol, all_points in history.items():
        delivery_month = delivery_month_from_symbol(symbol)
        if not delivery_month or month_distance(delivery_month, selected_month) < 0:
            continue
        current = next(
            (
                point
                for point in reversed(all_points)
                if point["date"] == selected_date and point["settlement"] is not None
            ),
            None,
        )
        if current is not None:
            candidates.append((delivery_month, symbol, all_points, current))
    candidates.sort(key=lambda item: (item[0], item[1]))
    if len(candidates) < 6:
        return None
    if month_distance(candidates[0][0], selected_month) > 1:
        return None

    front_six = candidates[:6]
    distant_delivery_month = add_months(front_six[0][0], 6)
    distant_candidate = next(
        (item for item in candidates if item[0] == distant_delivery_month), None
    )
    if distant_candidate is None:
        return None

    def make_contract(
        candidate: tuple[str, str, list[dict[str, Any]], dict[str, Any]],
        color: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        delivery_month, symbol, all_points, current = candidate
        points = [point for point in all_points if point["date"] <= selected_date][-days:]
        previous = next(
            (
                point["settlement"]
                for point in reversed(points[:-1])
                if point["settlement"] is not None
            ),
            None,
        )
        recent_volumes = [point["volume"] for point in points[-20:]]
        contract = {
            "symbol": symbol,
            "delivery_month": delivery_month,
            "last_trading_date": None,
            "settlement": current["settlement"],
            "previous_settlement": previous,
            "daily_change": pct_change(current["settlement"], previous),
            "last_price": None,
            "bid": current.get("bid"),
            "ask": current.get("ask"),
            "bid_ask_gap": (
                current["ask"] - current["bid"]
                if current.get("bid") is not None
                and current.get("ask") is not None
                else None
            ),
            "volume": current["volume"],
            "average_volume_20d": (
                statistics.mean(recent_volumes) if recent_volumes else 0
            ),
            "open_interest": current["open_interest"] or 0,
            "color": color,
        }
        return contract, {
            "symbol": symbol,
            "delivery_month": delivery_month,
            "color": color,
            "points": points,
        }

    contracts: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    for index, candidate in enumerate(front_six):
        contract, contract_series_data = make_contract(candidate, COLORS[index])
        contracts.append(contract)
        series.append(contract_series_data)

    distant_contract, distant_series = make_contract(distant_candidate, "#7A5AF8")
    front_by_date = {
        point["date"]: point["settlement"] for point in series[0]["points"]
    }
    spread_series: list[dict[str, Any]] = []
    for point in distant_series["points"]:
        front_price = front_by_date.get(point["date"])
        distant_price = point["settlement"]
        if front_price is None or distant_price is None:
            continue
        spread_series.append(
            {
                "date": point["date"],
                "spread": distant_price - front_price,
                "spread_percentage": distant_price / front_price - 1,
            }
        )

    selected_alerts = [
        item
        for item in alerts
        if item.get("business_date") == selected_date
        and item.get("rule") in DATA_QUALITY_ALERT_RULES
    ][:6]
    daily_changes = [
        item["daily_change"] for item in contracts if item["daily_change"] is not None
    ]
    estimate = make_estimate(contracts, series, distant_contract)
    return {
        "business_date": selected_date,
        "summary": {
            "front_symbol": contracts[0]["symbol"],
            "front_delivery_month": contracts[0]["delivery_month"],
            "front_settlement": contracts[0]["settlement"],
            "front_daily_change": contracts[0]["daily_change"],
            "distant_symbol": distant_contract["symbol"],
            "distant_delivery_month": distant_delivery_month,
            "distant_settlement": distant_contract["settlement"],
            "distant_daily_change": distant_contract["daily_change"],
            "current_spread": spread_series[-1]["spread"] if spread_series else None,
            "current_spread_percentage": (
                spread_series[-1]["spread_percentage"] if spread_series else None
            ),
            "total_volume": sum(item["volume"] for item in contracts),
            "total_open_interest": sum(item["open_interest"] for item in contracts),
            "breadth_up": sum(1 for value in daily_changes if value > 0),
            "breadth_down": sum(1 for value in daily_changes if value < 0),
            "two_sided_quote_count": sum(
                1 for item in contracts if item["bid_ask_gap"] is not None
            ),
        },
        "contracts": contracts,
        "series": series,
        "distant_contract": distant_contract,
        "distant_series": distant_series["points"],
        "spread_series": spread_series,
        "alerts": selected_alerts,
        "estimate": estimate,
    }


def compact_view(view: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in view.items()
        if key not in {"series", "distant_series", "spread_series"}
    }


def percentile_rank(value: float | None, history: list[float | None]) -> float | None:
    sample = [
        float(item)
        for item in history
        if item is not None and math.isfinite(float(item))
    ]
    if value is None or len(sample) < 20:
        return None
    return sum(item <= value for item in sample) / len(sample)


def quantile_nearest_rank(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(probability * len(ordered)) - 1)
    return ordered[index]


def calibrate_anomaly_days(
    views: dict[str, dict[str, Any]],
    target_rate: float = 0.05,
    feature_window: int = 60,
    calibration_window: int = 252,
    minimum_calibration: int = 60,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for business_date in sorted(views):
        view = views[business_date]
        summary = view["summary"]
        price_changes = [
            abs(item["daily_change"])
            for item in view["contracts"]
            if item["daily_change"] is not None
        ]
        row = {
            "date": business_date,
            "price": max(price_changes) if price_changes else None,
            "volume": float(summary["total_volume"]),
            "oi": float(summary["total_open_interest"]),
            "spread": summary["current_spread"],
        }
        row["oi_move"] = (
            abs(row["oi"] - previous["oi"]) if previous is not None else None
        )
        row["spread_move"] = (
            abs(row["spread"] - previous["spread"])
            if previous is not None
            and row["spread"] is not None
            and previous["spread"] is not None
            else None
        )
        rows.append(row)
        previous = row

    eligible = 0
    alerts = 0
    reason_counts = {"price": 0, "volume": 0, "oi": 0, "spread": 0}
    for index, row in enumerate(rows):
        feature_history = rows[max(0, index - feature_window) : index]
        components = {
            "price": percentile_rank(
                row["price"], [item["price"] for item in feature_history]
            ),
            "volume": percentile_rank(
                row["volume"], [item["volume"] for item in feature_history]
            ),
            "oi": percentile_rank(
                row["oi_move"], [item["oi_move"] for item in feature_history]
            ),
            "spread": percentile_rank(
                row["spread_move"], [item["spread_move"] for item in feature_history]
            ),
        }
        ranked = sorted(
            (value for value in components.values() if value is not None), reverse=True
        )
        row["components"] = components
        row["score"] = (
            sum(ranked[:2]) / min(2, len(ranked)) if ranked else None
        )
        calibration_scores = [
            item["score"]
            for item in rows[max(0, index - calibration_window) : index]
            if item.get("score") is not None
        ]
        if row["score"] is None or len(calibration_scores) < minimum_calibration:
            continue
        eligible += 1
        threshold = quantile_nearest_rank(calibration_scores, 1 - target_rate)
        row["threshold"] = threshold
        if row["score"] <= threshold:
            continue
        alerts += 1
        ranked_reasons = sorted(
            (
                (name, value)
                for name, value in components.items()
                if value is not None
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        primary_reason = ranked_reasons[0][0]
        reason_counts[primary_reason] += 1
        reason_labels = {
            "price": "价格变动",
            "volume": "成交量",
            "oi": "持仓变化",
            "spread": "远近月 Spread 变化",
        }
        reason_text = "；".join(
            f"{reason_labels[name]}处于近 {feature_window} 日第 {value:.0%} 百分位"
            for name, value in ranked_reasons[:2]
        )
        severity = (
            "critical"
            if row["score"]
            > quantile_nearest_rank(calibration_scores, 0.99)
            else "warning"
        )
        views[row["date"]]["alerts"] = [
            {
                "created_at": None,
                "business_date": row["date"],
                "symbol": None,
                "severity": severity,
                "rule": "calibrated_daily_anomaly",
                "message": f"历史校准异常日：{reason_text}",
                "current_value": row["score"],
                "reference_value": threshold,
                "score": row["score"],
                "details": {
                    "components": components,
                    "primary_reason": primary_reason,
                    "target_rate": target_rate,
                },
            }
        ] + views[row["date"]].get("alerts", [])

    return {
        "target_rate": target_rate,
        "feature_window": feature_window,
        "calibration_window": calibration_window,
        "eligible_days": eligible,
        "alert_days": alerts,
        "actual_rate": alerts / eligible if eligible else 0.0,
        "reason_counts": reason_counts,
    }


def build_payload(
    connection: sqlite3.Connection,
    business_date: str | None = None,
    days: int = 120,
    target_anomaly_rate: float = 0.05,
    anomaly_feature_window: int = 60,
    anomaly_calibration_window: int = 252,
    anomaly_minimum_calibration: int = 60,
) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    history = load_history_universe(connection)
    if not history:
        raise RuntimeError("No SGX Whole Milk Powder history available")
    alert_rows = connection.execute(
        """
        SELECT created_at, business_date, symbol, severity, rule, message,
               current_value, reference_value, score
        FROM alerts ORDER BY alert_id DESC
        """
    ).fetchall()
    all_alerts = [dict(row) for row in alert_rows]
    all_dates = sorted(
        {point["date"] for points in history.values() for point in points},
        reverse=True,
    )
    earliest_delivery_month = min(
        delivery_month
        for symbol in history
        if (delivery_month := delivery_month_from_symbol(symbol)) is not None
    )
    views: dict[str, dict[str, Any]] = {}
    for selected_date in all_dates:
        if selected_date[:7] < earliest_delivery_month:
            continue
        view = build_historical_view(history, selected_date, days, all_alerts)
        if view is None:
            continue
        views[selected_date] = compact_view(view)
    if not views:
        raise RuntimeError("No complete front/six-month historical views available")
    latest_snapshot_date, _ = load_latest_snapshot_quotes(connection)
    requested_date = business_date or max(
        date for date in (max(views), latest_snapshot_date) if date
    )
    eligible_target_dates = [date for date in views if date <= requested_date]
    target_date = (
        max(eligible_target_dates) if eligible_target_dates else max(views)
    )
    full_current_view = build_historical_view(
        history, target_date, days, all_alerts
    )
    if full_current_view is None:
        raise RuntimeError(f"Unable to rebuild complete view for {target_date}")
    quote_date, latest_quotes = load_latest_snapshot_quotes(
        connection, requested_date=requested_date
    )
    apply_snapshot_quotes(full_current_view, quote_date, latest_quotes)
    apply_snapshot_quotes(views[target_date], quote_date, latest_quotes)

    anomaly_stats = calibrate_anomaly_days(
        views,
        target_rate=target_anomaly_rate,
        feature_window=anomaly_feature_window,
        calibration_window=anomaly_calibration_window,
        minimum_calibration=anomaly_minimum_calibration,
    )
    full_current_view["alerts"] = views[target_date]["alerts"]
    lag = business_day_lag(target_date)
    generated_at = dt.datetime.now(ZoneInfo("Asia/Singapore")).isoformat(
        timespec="seconds"
    )
    return {
        "meta": {
            "title": "SGX-NZX Global Whole Milk Powder Futures",
            "business_date": target_date,
            "generated_at": generated_at,
            "contract_count": len(full_current_view["contracts"]),
            "history_days": days,
            "source_url": "https://www.sgx.com/derivatives/products/dairy?cc=WMP",
            "data_status": "正常" if lag <= 2 else "数据陈旧",
            "business_day_lag": lag,
            "volume_definition": "SGX total-volume：该合约当日累计成交手数",
            "open_interest_definition": "open-interest：未平仓合约总量，不是当日成交量",
        },
        "available_dates": list(views),
        "views": views,
        "history": history,
        "anomaly_stats": anomaly_stats,
        **full_current_view,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SGX Whole Milk Powder Futures Monitor</title>
  <style>
    :root {
      --navy:#101828; --ink:#344054; --muted:#667085; --line:#E4E7EC;
      --panel:#FFFFFF; --canvas:#F7F8FA; --blue:#175CD3; --blue-soft:#EFF4FF;
      --green:#067647; --green-soft:#ECFDF3; --red:#B42318; --red-soft:#FEF3F2;
      --amber:#B54708; --amber-soft:#FFFAEB; --shadow:0 1px 2px rgba(16,24,40,.04);
    }
    * { box-sizing:border-box; }
    body {
      margin:0; background:var(--canvas); color:var(--navy);
      font-family:Inter, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      font-variant-numeric:tabular-nums; -webkit-font-smoothing:antialiased;
    }
    .topbar { height:5px; background:linear-gradient(90deg,#0B4A6F,#175CD3 52%,#0E9384); }
    .shell { max-width:1440px; margin:0 auto; padding:28px 32px 44px; }
    header { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:24px; }
    .eyebrow { color:var(--blue); font-size:12px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; margin-bottom:7px; }
    h1 { font-size:27px; line-height:1.2; margin:0 0 8px; letter-spacing:-.025em; }
    .subtitle { color:var(--muted); font-size:13px; }
    .meta { text-align:right; color:var(--muted); font-size:12px; line-height:1.8; }
    .meta-head { display:flex; align-items:center; justify-content:flex-end; gap:10px; margin-bottom:2px; }
    .status { display:inline-flex; align-items:center; gap:7px; color:var(--green); font-weight:650; }
    .dot { width:7px; height:7px; border-radius:50%; background:currentColor; box-shadow:0 0 0 3px rgba(6,118,71,.12); }
    .refresh-button { display:inline-flex; align-items:center; gap:5px; height:29px; padding:0 10px; border:1px solid #98A2B3; border-radius:7px; color:#344054; background:#fff; font:650 11px inherit; cursor:pointer; box-shadow:0 1px 2px rgba(16,24,40,.05); }
    .refresh-button:hover { color:var(--blue); border-color:#84ADFF; background:#F5F8FF; }
    .refresh-button:disabled { color:#98A2B3; cursor:wait; background:#F9FAFB; }
    .asof-bar { display:flex; align-items:center; justify-content:space-between; gap:18px; margin-bottom:14px; padding:12px 14px; background:#EEF4FF; border:1px solid #D1E0FF; border-radius:9px; }
    .asof-copy { display:flex; align-items:center; gap:10px; min-width:0; }
    .asof-icon { display:grid; place-items:center; width:29px; height:29px; flex:0 0 auto; border-radius:7px; color:var(--blue); background:#fff; border:1px solid #D1E0FF; font-size:15px; }
    .asof-title { color:#1849A9; font-size:12px; font-weight:750; }
    .asof-note { color:#475467; font-size:10px; margin-top:2px; }
    .asof-control { display:flex; align-items:center; gap:9px; flex:0 0 auto; }
    .asof-control label { color:#344054; font-size:11px; font-weight:650; }
    .asof-control select { min-width:160px; height:34px; padding:0 34px 0 11px; color:var(--navy); background:#fff; border:1px solid #98A2B3; border-radius:7px; font:650 12px inherit; cursor:pointer; }
    .kpis { display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:12px; margin-bottom:18px; }
    .kpi, .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; box-shadow:var(--shadow); }
    .kpi { padding:15px 16px 14px; min-height:92px; }
    .kpi-label { color:var(--muted); font-size:11px; font-weight:650; letter-spacing:.03em; margin-bottom:10px; }
    .kpi-value { font-size:21px; font-weight:700; letter-spacing:-.025em; line-height:1; }
    .kpi-sub { margin-top:8px; color:var(--muted); font-size:11px; }
    .positive { color:var(--green)!important; } .negative { color:var(--red)!important; } .neutral { color:var(--amber)!important; }
    .grid { display:grid; grid-template-columns:minmax(0,1.58fr) minmax(340px,.72fr); gap:18px; }
    .left { display:grid; gap:18px; min-width:0; }
    .panel { overflow:hidden; }
    .panel-head { display:flex; align-items:flex-start; justify-content:space-between; gap:20px; padding:18px 20px 14px; border-bottom:1px solid #F2F4F7; }
    .panel-title { font-size:15px; font-weight:700; margin:0 0 5px; }
    .panel-note { color:var(--muted); font-size:11px; line-height:1.55; }
    .range { display:flex; padding:3px; background:#F2F4F7; border-radius:7px; }
    .range button { border:0; background:transparent; color:var(--muted); padding:5px 9px; border-radius:5px; font:600 11px inherit; cursor:pointer; }
    .range button.active { background:#fff; color:var(--navy); box-shadow:0 1px 2px rgba(16,24,40,.12); }
    .chart-wrap { height:286px; padding:12px 14px 5px; position:relative; }
    canvas { width:100%; height:100%; display:block; }
    .legend { display:flex; flex-wrap:wrap; gap:8px 16px; padding:0 20px 16px; }
    .legend-item { display:flex; align-items:center; gap:6px; color:var(--ink); font-size:11px; }
    .swatch { width:8px; height:8px; border-radius:2px; }
    .contract-strip { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); border-top:1px solid #F2F4F7; }
    .contract-cell { padding:12px 12px 13px; border-right:1px solid #F2F4F7; min-width:0; }
    .contract-cell:last-child { border-right:0; }
    .contract-code { font-size:11px; font-weight:700; white-space:nowrap; }
    .contract-price { font-size:14px; font-weight:700; margin-top:5px; }
    .contract-change { font-size:10px; margin-top:3px; }
    .quote-divider { height:1px; margin:10px 0 8px; background:#EAECF0; }
    .quote-row { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:5px; }
    .quote-item { min-width:0; }
    .quote-label { color:#98A2B3; font-size:8px; font-weight:700; letter-spacing:.05em; text-transform:uppercase; }
    .quote-value { color:var(--ink); font-size:10px; font-weight:650; margin-top:3px; overflow:hidden; text-overflow:ellipsis; }
    .quote-value.gap { color:var(--blue); }
    .quote-missing { color:#98A2B3!important; font-weight:500; }
    .spread-section { border-top:1px solid #F2F4F7; background:#FCFCFD; }
    .spread-head { display:flex; justify-content:space-between; align-items:flex-end; gap:18px; padding:15px 20px 2px; }
    .spread-title { color:var(--ink); font-size:12px; font-weight:700; }
    .spread-note { color:var(--muted); font-size:10px; margin-top:4px; }
    .spread-current { text-align:right; }
    .spread-value { font-size:18px; font-weight:750; }
    .spread-percent { color:var(--muted); font-size:10px; margin-top:3px; }
    .spread-chart-wrap { height:190px; padding:5px 14px 7px; position:relative; }
    .insights { display:flex; flex-direction:column; align-self:start; }
    .estimate { margin:18px; padding:18px; border-radius:9px; background:linear-gradient(135deg,#F0F5FF,#F8FAFC); border:1px solid #D1E0FF; }
    .estimate-label { color:var(--blue); font-size:10px; font-weight:800; letter-spacing:.11em; text-transform:uppercase; }
    .estimate h2 { font-size:19px; line-height:1.4; letter-spacing:-.02em; margin:9px 0 12px; }
    .estimate-tags { display:flex; gap:8px; margin-bottom:15px; }
    .tag { display:inline-flex; padding:5px 8px; border-radius:6px; background:#fff; border:1px solid #D0D5DD; font-size:11px; font-weight:650; }
    .reasons { margin:0; padding:0; list-style:none; display:grid; gap:9px; }
    .reasons li { position:relative; padding-left:14px; color:var(--ink); font-size:12px; line-height:1.45; }
    .reasons li::before { content:""; position:absolute; left:0; top:.5em; width:5px; height:5px; background:var(--blue); border-radius:50%; }
    .section-label { padding:0 20px 10px; color:var(--muted); font-size:10px; font-weight:750; letter-spacing:.09em; text-transform:uppercase; }
    .alert-list { padding:0 18px 18px; display:grid; gap:9px; }
    .alert { display:grid; grid-template-columns:8px 1fr; gap:10px; padding:11px 12px; border:1px solid var(--line); border-radius:8px; }
    .alert-mark { width:7px; height:7px; border-radius:50%; margin-top:4px; background:var(--green); }
    .alert.warning .alert-mark { background:#F79009; } .alert.critical .alert-mark { background:#D92D20; } .alert.info .alert-mark { background:#2E90FA; }
    .alert-title { font-size:11px; font-weight:700; margin-bottom:3px; }
    .alert-meta { color:#98A2B3; font-size:9px; margin-bottom:5px; }
    .alert-text { color:var(--muted); font-size:11px; line-height:1.4; }
    .alert-business { margin-top:9px; padding:9px 10px; border-radius:6px; background:#F8FAFC; border-left:2px solid #84ADFF; color:#344054; font-size:10px; line-height:1.5; }
    .alert-business strong { color:var(--blue); font-size:9px; letter-spacing:.04em; }
    .alert-check { margin-top:5px; color:#667085; font-size:9px; line-height:1.45; }
    .method { margin:0 18px 18px; border-top:1px solid var(--line); padding-top:14px; color:var(--muted); font-size:10px; line-height:1.55; }
    footer { display:flex; justify-content:space-between; gap:20px; margin-top:18px; color:#98A2B3; font-size:10px; }
    .tooltip { display:none; position:fixed; z-index:20; pointer-events:none; background:#101828; color:#fff; padding:8px 10px; border-radius:6px; font-size:10px; line-height:1.55; box-shadow:0 4px 12px rgba(16,24,40,.2); }
    @media (max-width:1050px) {
      .kpis { grid-template-columns:repeat(4,1fr); }
      .grid { grid-template-columns:1fr; }
    }
    @media (max-width:720px) {
      .shell { padding:20px 14px 32px; } header { flex-direction:column; } .meta { text-align:left; } .meta-head { justify-content:flex-start; }
      .asof-bar { align-items:flex-start; flex-direction:column; }
      .asof-control { width:100%; justify-content:space-between; }
      .asof-control select { flex:1; }
      .kpis { grid-template-columns:repeat(2,1fr); }
      .contract-strip { grid-template-columns:repeat(3,1fr); }
      .contract-cell:nth-child(3) { border-right:0; }
      .contract-cell:nth-child(-n+3) { border-bottom:1px solid #F2F4F7; }
      .panel-head { padding:16px; } .chart-wrap { height:250px; }
    }
  </style>
</head>
<body>
<div class="topbar"></div>
<main class="shell">
  <header>
    <div>
      <div class="eyebrow">Daily Market Monitor · WMP</div>
      <h1>SGX Whole Milk Powder Futures Dashboard</h1>
      <div class="subtitle">最近六个活跃合约 · 结算价、成交量、期限结构与异常信号</div>
    </div>
    <div class="meta">
      <div class="meta-head">
        <div class="status" id="dataStatus"><span class="dot"></span><span></span></div>
        <button class="refresh-button" id="refreshButton" type="button" title="绕过浏览器缓存，重新载入最新已发布页面">↻ 刷新页面</button>
      </div>
      <div>市场业务日 <strong id="businessDate"></strong></div>
      <div>生成时间 <span id="generatedAt"></span></div>
    </div>
  </header>

  <section class="asof-bar">
    <div class="asof-copy">
      <div class="asof-icon">↶</div>
      <div>
        <div class="asof-title">历史回溯</div>
        <div class="asof-note">选择历史交易日，近月、+6M 远月、价格、成交量、Spread 与判断将同步滚动</div>
      </div>
    </div>
    <div class="asof-control">
      <label for="asOfDate">回溯日期</label>
      <select id="asOfDate"></select>
    </div>
  </section>

  <section class="kpis" id="kpis"></section>

  <div class="grid">
    <div class="left">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">01 · 日度结算价走势</h2>
            <div class="panel-note">前六个交割月；价格口径为 daily settlement price</div>
          </div>
          <div class="range" data-chart="price">
            <button data-days="30">30D</button><button data-days="60" class="active">60D</button><button data-days="120">120D</button>
          </div>
        </div>
        <div class="chart-wrap"><canvas id="priceChart"></canvas></div>
        <div class="legend" id="priceLegend"></div>
        <div class="section-label" id="quoteLabel" style="padding-top:2px"></div>
        <div class="contract-strip" id="contractStrip"></div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">02 · 日度成交量与远近月 Spread</h2>
            <div class="panel-note">成交量为六个近月合约的 total-volume；Spread = 6 个月远月结算价 − 近月结算价</div>
          </div>
          <div class="range" data-chart="volume">
            <button data-days="30" class="active">30D</button><button data-days="60">60D</button><button data-days="120">120D</button>
          </div>
        </div>
        <div class="chart-wrap"><canvas id="volumeChart"></canvas></div>
        <div class="legend" id="volumeLegend"></div>
        <div class="spread-section">
          <div class="spread-head">
            <div>
              <div class="spread-title" id="spreadTitle"></div>
              <div class="spread-note">负值代表近月升水；正值代表远月升水</div>
            </div>
            <div class="spread-current">
              <div class="spread-value" id="spreadValue"></div>
              <div class="spread-percent" id="spreadPercent"></div>
            </div>
          </div>
          <div class="spread-chart-wrap"><canvas id="spreadChart"></canvas></div>
        </div>
      </section>
    </div>

    <aside class="panel insights">
      <div class="panel-head">
        <div>
          <h2 class="panel-title">03 · Insight & Alert</h2>
          <div class="panel-note">规则告警 + 基于历史的方向判断</div>
        </div>
      </div>
      <div class="estimate" id="estimate"></div>
      <div class="section-label">Active alerts</div>
      <div class="alert-list" id="alerts"></div>
      <div class="method" id="method"></div>
    </aside>
  </div>

  <footer>
    <span>Source: SGX public derivatives data · Contract code WMP</span>
    <span>For monitoring and research use only</span>
  </footer>
</main>
<div class="tooltip" id="tooltip"></div>
<script>
const DATA = __DATA__;
const fmt = new Intl.NumberFormat('en-US', {maximumFractionDigits: 1});
const pct = v => v == null ? '—' : `${v >= 0 ? '+' : ''}${(v*100).toFixed(2)}%`;
const cls = v => v == null || Math.abs(v) < .000001 ? 'neutral' : v > 0 ? 'positive' : 'negative';
const shortDate = s => s ? s.slice(5).replace('-', '/') : '—';
const monthLabel = s => s ? s.replace('-', '/') : '—';

document.getElementById('generatedAt').textContent = DATA.meta.generated_at.replace('T',' ').slice(0,16);
const state = {priceDays:60, volumeDays:30};
const tooltip = document.getElementById('tooltip');
const chartHit = new Map();
let VIEW = null;

function hydrateView(date) {
  const base = DATA.views[date];
  const pointsFor = symbol => (DATA.history[symbol] || []).filter(p => p.date <= date).slice(-DATA.meta.history_days);
  const series = base.contracts.map(c => ({symbol:c.symbol, delivery_month:c.delivery_month, color:c.color, points:pointsFor(c.symbol)}));
  const distantSeries = pointsFor(base.summary.distant_symbol);
  const frontByDate = new Map(series[0].points.map(p => [p.date,p.settlement]));
  const spreadSeries = distantSeries.flatMap(p => {
    const front = frontByDate.get(p.date);
    if (front == null || p.settlement == null) return [];
    return [{date:p.date, spread:p.settlement-front, spread_percentage:p.settlement/front-1}];
  });
  return {...base, series, distant_series:distantSeries, spread_series:spreadSeries};
}

function renderViewText() {
  const s = VIEW.summary, status = document.getElementById('dataStatus');
  document.getElementById('businessDate').textContent = VIEW.business_date;
  status.classList.remove('negative','neutral');
  const isLatest = VIEW.business_date === DATA.meta.business_date;
  status.querySelector('span:last-child').textContent = isLatest ? `数据${DATA.meta.data_status}` : '历史回溯';
  if (isLatest && DATA.meta.data_status !== '正常') status.classList.add('negative');
  if (!isLatest) status.classList.add('neutral');
  const kpis = [
    ['近月合约', s.front_symbol, monthLabel(s.front_delivery_month)],
    ['近月结算价', fmt.format(s.front_settlement), `<span class="${cls(s.front_daily_change)}">${pct(s.front_daily_change)} 日变动</span>`],
    ['远月合约 · +6M', s.distant_symbol || '—', monthLabel(s.distant_delivery_month)],
    ['远月结算价', s.distant_settlement == null ? '—' : fmt.format(s.distant_settlement), `<span class="${cls(s.distant_daily_change)}">${pct(s.distant_daily_change)} 日变动</span>`],
    ['六合约成交量', fmt.format(s.total_volume), '当日累计成交手数'],
    ['六合约未平仓量', fmt.format(s.total_open_interest), 'Open interest · 非成交量'],
    ['上涨 / 下跌', `${s.breadth_up} / ${s.breadth_down}`, '六个近月合约市场宽度'],
    ['有效双边报价', `${s.two_sided_quote_count} / 6`, 'Bid 与 Ask 同时存在']
  ];
  document.getElementById('kpis').innerHTML = kpis.map(x => `<div class="kpi"><div class="kpi-label">${x[0]}</div><div class="kpi-value">${x[1]}</div><div class="kpi-sub">${x[2]}</div></div>`).join('');
  const legendHTML = VIEW.contracts.map(c => `<div class="legend-item"><span class="swatch" style="background:${c.color}"></span>${c.symbol} · ${monthLabel(c.delivery_month)}</div>`).join('');
  document.getElementById('priceLegend').innerHTML = legendHTML;
  document.getElementById('volumeLegend').innerHTML = legendHTML;
  document.getElementById('spreadTitle').textContent = `${s.distant_symbol || '远月'} − ${s.front_symbol} 日度结算价差`;
  document.getElementById('spreadValue').textContent = s.current_spread == null ? '—' : `${s.current_spread >= 0 ? '+' : ''}${fmt.format(s.current_spread)}`;
  document.getElementById('spreadValue').className = `spread-value ${cls(s.current_spread)}`;
  document.getElementById('spreadPercent').textContent = `相对近月 ${pct(s.current_spread_percentage)}`;
  document.getElementById('quoteLabel').textContent =
    `Settlement & Bid-Ask Gap · Quote ${s.quote_date || VIEW.business_date} · Gap = Ask − Bid`;
  document.getElementById('contractStrip').innerHTML = VIEW.contracts.map(c => `
    <div class="contract-cell">
      <div class="contract-code" style="color:${c.color}">${c.symbol}</div>
      <div class="contract-price">${c.settlement == null ? '—' : fmt.format(c.settlement)}</div>
      <div class="contract-change ${cls(c.daily_change)}">${pct(c.daily_change)}</div>
      <div class="quote-divider"></div>
      <div class="quote-row">
        <div class="quote-item"><div class="quote-label">Bid</div><div class="quote-value ${c.bid==null?'quote-missing':''}">${c.bid==null?'—':fmt.format(c.bid)}</div></div>
        <div class="quote-item"><div class="quote-label">Ask</div><div class="quote-value ${c.ask==null?'quote-missing':''}">${c.ask==null?'—':fmt.format(c.ask)}</div></div>
        <div class="quote-item"><div class="quote-label">Gap</div><div class="quote-value gap ${c.bid_ask_gap==null?'quote-missing':''}">${c.bid_ask_gap==null?'—':fmt.format(c.bid_ask_gap)}</div></div>
      </div>
    </div>`).join('');
  const e = VIEW.estimate;
  document.getElementById('estimate').innerHTML = `
    <div class="estimate-label">Best guesstimate</div>
    <h2>${e.headline}</h2>
    <div class="estimate-tags"><span class="tag ${e.direction_class}">方向：${e.direction}</span><span class="tag">置信度：${e.confidence}</span></div>
    <ul class="reasons">${e.rationale.map(x=>`<li>${x}</li>`).join('')}</ul>`;
  const alertBox = document.getElementById('alerts');
  const alertTitle = rule => ({
    calibrated_daily_anomaly:'历史校准异常日',
    settlement_return:'结算价异常',
    volume_spike:'成交量异常',
    open_interest_change:'持仓变化异常',
    curve_spread:'期限价差异常',
    preliminary_settlement_gap:'初步与最终结算价偏差',
    missing_settlement:'结算价缺失',
    stale_market_data:'市场数据滞后',
    duplicate_contract:'合约记录重复',
    contract_count_drop:'合约数量异常减少',
    schema_field_removed:'数据字段缺失',
    schema_field_added:'新增数据字段',
    new_contract:'新合约出现',
    contract_removed:'合约不再返回'
  }[rule] || rule.replaceAll('_',' '));
  const reasonMeaning = reason => ({
    price:{
      meaning:'价格波动显著偏离近期常态，可能代表供需预期被快速重定价、突发基本面消息，或低流动性下的价格跳变。',
      check:'结合成交量和 Bid-Ask Gap；放量且价差稳定时，价格信号通常更可信。'
    },
    volume:{
      meaning:'成交量异常放大，可能代表新信息触发集中交易，也可能是临近交割时的换月或头寸迁移；本身不代表明确涨跌方向。',
      check:'结合持仓变化判断：量升仓增偏向新资金入场，量升仓降更可能是平仓或换月。'
    },
    oi:{
      meaning:'未平仓量变化异常，说明市场风险敞口正在快速建立或退出，可能对应新资金入场、集中平仓或交割前移仓。',
      check:'观察持仓变化方向，并结合价格：价涨仓增偏多头建立，价跌仓增偏空头建立。'
    },
    spread:{
      meaning:'远近月价差快速变化，可能反映现货松紧、库存与融资成本预期改变，或换月交易推动期限曲线重新定价。',
      check:'持续的近月升水通常偏向现货端紧张；远月升水扩大则更可能反映供应改善或持有成本。'
    }
  }[reason]);
  const alertMeaning = a => {
    if (a.rule === 'calibrated_daily_anomaly') {
      const primary = a.details && a.details.primary_reason;
      return reasonMeaning(primary) || {
        meaning:'多个市场指标同步偏离各自近期分布，可能意味着市场进入新的定价或流动性状态。',
        check:'拆分查看价格、成交量、持仓和 Spread，确认异常由哪一项主导。'
      };
    }
    if (a.rule === 'settlement_return') {
      const direction = a.current_value > 0 ? '向上' : a.current_value < 0 ? '向下' : '';
      return {
        meaning:`结算价出现异常${direction}重定价，可能对应供需预期变化、突发消息或流动性不足；单日跳动不一定形成持续趋势。`,
        check:'结合成交量、持仓以及次日价格是否延续进行确认。'
      };
    }
    if (a.rule === 'volume_spike') return reasonMeaning('volume');
    if (a.rule === 'open_interest_change') {
      const establishing = a.current_value > 0;
      return {
        meaning:establishing
          ? '未平仓量异常增加，通常代表新头寸或新增风险敞口进入市场。'
          : '未平仓量异常减少，通常代表集中平仓、到期退出或跨月移仓。',
        check:'结合价格方向与成交量，区分趋势性建仓和技术性换月。'
      };
    }
    if (a.rule === 'curve_spread') return reasonMeaning('spread');
    if (a.rule === 'preliminary_settlement_gap') return {
      meaning:'初步与最终结算价差异较大，可能来自收盘后修订、流动性较低或定价样本变化。',
      check:'以最终结算价为准，并检查该偏差是否在多个合约同步出现。'
    };
    if (['missing_settlement','stale_market_data','duplicate_contract','contract_count_drop','schema_field_removed'].includes(a.rule)) return {
      meaning:'这更可能是数据完整性或数据源异常，不应直接解读为市场供需信号。',
      check:'先核对 SGX 原始页面与下一次采集结果，再用于交易或风险判断。'
    };
    if (['schema_field_added','new_contract','contract_removed'].includes(a.rule)) return {
      meaning:'这通常反映合约生命周期或 SGX 数据结构变化，不代表价格方向。',
      check:'确认是否为正常挂牌、到期摘牌或接口字段调整。'
    };
    return {
      meaning:'该指标偏离近期常态，可能反映市场状态或数据状态发生变化。',
      check:'结合价格、成交量、持仓、期限结构及原始数据交叉确认。'
    };
  };
  const renderAlert = a => {
    const insight = alertMeaning(a);
    const meta = [a.business_date, a.symbol].filter(Boolean).join(' · ');
    return `<div class="alert ${a.severity}">
      <span class="alert-mark"></span>
      <div>
        <div class="alert-title">${alertTitle(a.rule)}</div>
        ${meta ? `<div class="alert-meta">${meta}</div>` : ''}
        <div class="alert-text">${a.message}</div>
        <div class="alert-business"><strong>业务含义</strong><br>${insight.meaning}<div class="alert-check"><strong>观察重点：</strong>${insight.check}</div></div>
      </div>
    </div>`;
  };
  alertBox.innerHTML = VIEW.alerts.length
    ? VIEW.alerts.map(renderAlert).join('')
    : `<div class="alert"><span class="alert-mark"></span><div><div class="alert-title">No statistical anomaly</div><div class="alert-text">该交易日的综合异常分数未超过动态 95 分位门槛。</div><div class="alert-business"><strong>业务含义</strong><br>价格、成交量、持仓与期限价差整体处于历史常态区间，没有出现足够强的状态切换信号。<div class="alert-check"><strong>注意：</strong>无统计异常不等于无市场风险，低流动性也可能降低异常识别能力。</div></div></div></div>`;
  const stats=DATA.anomaly_stats;
  document.getElementById('method').innerHTML = `${e.disclaimer}<br><br><strong>Alert 展示口径：</strong>仅展示所选交易日的综合市场异常，以及影响数据可靠性的重大质量告警。逐合约价格、成交量、持仓与 Spread 诊断已并入综合分数，不再重复逐条告警。<br><br><strong>异常校准：</strong>目标 ${(stats.target_rate*100).toFixed(1)}%；历史回测 ${stats.alert_days}/${stats.eligible_days} 日（${(stats.actual_rate*100).toFixed(2)}%）。每日综合价格、成交量、持仓及 Spread 的近 ${stats.feature_window} 日排名，并以过去 ${stats.calibration_window} 日约 95 分位为门槛。<br><br><strong>成交量口径：</strong>${DATA.meta.volume_definition}<br><strong>持仓口径：</strong>${DATA.meta.open_interest_definition}`;
}

function setupCanvas(canvas) {
  const rect = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width*dpr));
  canvas.height = Math.max(1, Math.floor(rect.height*dpr));
  const ctx = canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  return {ctx,w:rect.width,h:rect.height};
}
function niceMax(v) {
  if (!v) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v))), n = v/p;
  return (n<=1?1:n<=2?2:n<=5?5:10)*p;
}
function datesFor(days) {
  const all = [...new Set(VIEW.series.flatMap(s=>s.points.map(p=>p.date)))].sort();
  return all.slice(-days);
}
function axes(ctx,w,h,min,max,dates,formatter) {
  const m={l:52,r:14,t:12,b:28}, pw=w-m.l-m.r, ph=h-m.t-m.b;
  ctx.font='10px Segoe UI'; ctx.fillStyle='#98A2B3'; ctx.strokeStyle='#EAECF0'; ctx.lineWidth=1;
  for(let i=0;i<5;i++){ const y=m.t+ph*i/4, val=max-(max-min)*i/4; ctx.beginPath();ctx.moveTo(m.l,y);ctx.lineTo(w-m.r,y);ctx.stroke();ctx.fillText(formatter(val),4,y+3); }
  const ticks=Math.min(6,dates.length);
  for(let i=0;i<ticks;i++){ const ix=Math.round((dates.length-1)*i/Math.max(1,ticks-1)), x=m.l+pw*ix/Math.max(1,dates.length-1); ctx.fillText(shortDate(dates[ix]),x-12,h-8); }
  return {m,pw,ph,x:i=>m.l+pw*i/Math.max(1,dates.length-1),y:v=>m.t+ph*(max-v)/Math.max(.0001,max-min)};
}
function drawPrice() {
  const canvas=document.getElementById('priceChart'), {ctx,w,h}=setupCanvas(canvas), dates=datesFor(state.priceDays);
  const mapIndex=new Map(dates.map((d,i)=>[d,i]));
  const vals=VIEW.series.flatMap(s=>s.points.filter(p=>mapIndex.has(p.date)&&p.settlement!=null).map(p=>p.settlement));
  if(!vals.length)return;
  let min=Math.min(...vals),max=Math.max(...vals),pad=(max-min)*.08||10;min-=pad;max+=pad;
  const a=axes(ctx,w,h,min,max,dates,v=>fmt.format(v));
  const hits=[];
  VIEW.series.forEach(series=>{
    const byDate=new Map(series.points.map(p=>[p.date,p]));ctx.strokeStyle=series.color;ctx.lineWidth=1.8;ctx.beginPath();let started=false;
    dates.forEach((d,i)=>{const p=byDate.get(d);if(!p||p.settlement==null)return;const x=a.x(i),y=a.y(p.settlement);started?ctx.lineTo(x,y):(ctx.moveTo(x,y),started=true);hits.push({x,y,date:d,value:p.settlement,symbol:series.symbol,color:series.color,type:'price'});});
    ctx.stroke();
  });
  chartHit.set(canvas,hits);
}
function drawVolume() {
  const canvas=document.getElementById('volumeChart'), {ctx,w,h}=setupCanvas(canvas), dates=datesFor(state.volumeDays);
  const dateTotals=dates.map(d=>VIEW.series.reduce((sum,s)=>sum+(s.points.find(p=>p.date===d)?.volume||0),0));
  const max=niceMax(Math.max(...dateTotals,1)), a=axes(ctx,w,h,0,max,dates,v=>fmt.format(v));
  const bar=Math.max(2,Math.min(14,a.pw/Math.max(1,dates.length)*.68)), hits=[];
  dates.forEach((d,i)=>{let base=0;VIEW.series.forEach(series=>{const p=series.points.find(x=>x.date===d),v=p?.volume||0;if(v<=0)return;const y1=a.y(base+v),y0=a.y(base);ctx.fillStyle=series.color;ctx.fillRect(a.x(i)-bar/2,y1,bar,Math.max(1,y0-y1));hits.push({x:a.x(i),y:y1,w:bar,h:y0-y1,date:d,value:v,symbol:series.symbol,color:series.color,type:'volume'});base+=v;});});
  chartHit.set(canvas,hits);
}
function drawSpread() {
  const canvas=document.getElementById('spreadChart'), {ctx,w,h}=setupCanvas(canvas), dates=datesFor(state.volumeDays);
  const byDate=new Map(VIEW.spread_series.map(p=>[p.date,p]));
  const values=dates.map(d=>byDate.get(d)?.spread).filter(v=>v!=null);
  if(!values.length)return;
  let min=Math.min(...values),max=Math.max(...values),pad=(max-min)*.12||Math.max(10,Math.abs(max)*.08);
  min-=pad;max+=pad;
  const a=axes(ctx,w,h,min,max,dates,v=>`${v>=0?'+':''}${fmt.format(v)}`);
  if(min<0&&max>0){ctx.strokeStyle='#D0D5DD';ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(a.m.l,a.y(0));ctx.lineTo(w-a.m.r,a.y(0));ctx.stroke();ctx.setLineDash([]);}
  ctx.strokeStyle='#7A5AF8';ctx.lineWidth=2;ctx.beginPath();let started=false;const hits=[];
  const summary=VIEW.summary;
  dates.forEach((d,i)=>{const p=byDate.get(d);if(!p)return;const x=a.x(i),y=a.y(p.spread);started?ctx.lineTo(x,y):(ctx.moveTo(x,y),started=true);hits.push({x,y,date:d,value:p.spread,percentage:p.spread_percentage,symbol:`${summary.distant_symbol} − ${summary.front_symbol}`,color:'#7A5AF8',type:'spread'});});
  ctx.stroke();chartHit.set(canvas,hits);
}
function bindTooltip(canvas) {
  canvas.addEventListener('mousemove',ev=>{const r=canvas.getBoundingClientRect(),x=ev.clientX-r.left,y=ev.clientY-r.top,hits=chartHit.get(canvas)||[];
    let hit=null,best=18;for(const p of hits){const d=p.type==='volume'?(Math.abs(x-p.x)<Math.max(7,p.w/2+3)&&y>=p.y-3&&y<=p.y+p.h+3?0:99):Math.hypot(x-p.x,y-p.y);if(d<best){best=d;hit=p;}}
    if(!hit){tooltip.style.display='none';return;} const label=hit.type==='price'?'结算价':hit.type==='volume'?'成交量':'Spread'; const extra=hit.type==='spread'?`<br>相对近月：${pct(hit.percentage)}`:''; tooltip.innerHTML=`<strong>${hit.symbol}</strong><br>${hit.date}<br>${label}：${hit.value>=0&&hit.type==='spread'?'+':''}${fmt.format(hit.value)}${extra}`;tooltip.style.display='block';tooltip.style.left=`${ev.clientX+12}px`;tooltip.style.top=`${ev.clientY+12}px`;});
  canvas.addEventListener('mouseleave',()=>tooltip.style.display='none');
}
document.querySelectorAll('.range button').forEach(btn=>btn.addEventListener('click',()=>{
  const group=btn.closest('.range');group.querySelectorAll('button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');
  const days=Number(btn.dataset.days);if(group.dataset.chart==='price'){state.priceDays=days;drawPrice();}else{state.volumeDays=days;drawVolume();drawSpread();}
}));
bindTooltip(document.getElementById('priceChart'));bindTooltip(document.getElementById('volumeChart'));bindTooltip(document.getElementById('spreadChart'));
function render(){drawPrice();drawVolume();drawSpread();}
function applyView(date) {
  VIEW = hydrateView(date);
  renderViewText();
  render();
}
const asOfSelect = document.getElementById('asOfDate');
asOfSelect.innerHTML = DATA.available_dates.map((date,index) => `<option value="${date}">${date}${index===0?' · 最新':''}</option>`).join('');
const requestedAsOf = new URL(window.location.href).searchParams.get('asof');
const initialDate = DATA.available_dates.includes(requestedAsOf) ? requestedAsOf : DATA.meta.business_date;
asOfSelect.value = initialDate;
asOfSelect.addEventListener('change',()=>applyView(asOfSelect.value));
document.getElementById('refreshButton').addEventListener('click', event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = '↻ 刷新中';
  const url = new URL(window.location.href);
  url.searchParams.set('_refresh', Date.now().toString());
  url.searchParams.set('asof', asOfSelect.value);
  window.location.replace(url.toString());
});
applyView(initialDate);
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(render,100);});
</script>
</body>
</html>
"""


def generate_dashboard(
    connection: sqlite3.Connection,
    output_path: Path,
    business_date: str | None = None,
    days: int = 120,
    target_anomaly_rate: float = 0.05,
    anomaly_feature_window: int = 60,
    anomaly_calibration_window: int = 252,
    anomaly_minimum_calibration: int = 60,
) -> Path:
    payload = build_payload(
        connection,
        business_date=business_date,
        days=days,
        target_anomaly_rate=target_anomaly_rate,
        anomaly_feature_window=anomaly_feature_window,
        anomaly_calibration_window=anomaly_calibration_window,
        anomaly_minimum_calibration=anomaly_minimum_calibration,
    )
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        HTML_TEMPLATE.replace("__DATA__", serialized), encoding="utf-8"
    )
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="data/sgx_wmp.sqlite3")
    parser.add_argument("--output", default="dashboard.html")
    parser.add_argument("--business-date")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--target-anomaly-rate", type=float, default=0.05)
    parser.add_argument("--anomaly-feature-window", type=int, default=60)
    parser.add_argument("--anomaly-calibration-window", type=int, default=252)
    parser.add_argument("--anomaly-minimum-calibration", type=int, default=60)
    args = parser.parse_args()
    database = Path(args.database).resolve()
    output = Path(args.output).resolve()
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        path = generate_dashboard(
            connection,
            output,
            business_date=args.business_date,
            days=args.days,
            target_anomaly_rate=args.target_anomaly_rate,
            anomaly_feature_window=args.anomaly_feature_window,
            anomaly_calibration_window=args.anomaly_calibration_window,
            anomaly_minimum_calibration=args.anomaly_minimum_calibration,
        )
    finally:
        connection.close()
    print(f"OK dashboard={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
