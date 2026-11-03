#!/usr/bin/env python3
"""GMO Coin leverage trading bot for BTC_JPY and other symbols.

- Opens long and short positions without using market orders.
- Evaluates closed 15-minute candles and the order book once per hour.
- Processes multiple symbols sequentially with one position and one limit order per symbol.
- Prevents additional positions in the same symbol and separates dry-run and live states.
- Loads settings and API keys from .env beside this file.
- This reference implementation does not guarantee profits.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


ENV_FILE = Path(__file__).resolve().with_name(".env")
D0 = Decimal("0")
D1 = Decimal("1")
D100 = Decimal("100")
UTC = timezone.utc
JST = ZoneInfo("Asia/Tokyo")
ACTIVE_ORDER_STATUSES = {"WAITING", "ORDERED", "MODIFYING", "CANCELLING"}
FINAL_ORDER_STATUSES = {"CANCELED", "EXECUTED", "EXPIRED"}
LOGGER = logging.getLogger("gmo_bot")


def dec(value: Any) -> Decimal:
    return Decimal(str(value))


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result if result.tzinfo else result.replace(tzinfo=UTC)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_dec(name: str, default: str) -> Decimal:
    return dec(os.getenv(name, default))


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


class GMOAPIError(RuntimeError):
    """Raised when the HTTP/API response explicitly reports failure."""


@dataclass(frozen=True)
class Config:
    dry_run: bool
    symbol: str
    symbols: tuple[str, ...]
    interval_seconds: int
    kline_count: int
    kline_interval: str
    strategy: str
    ema_short: int
    ema_long: int
    sma_short: int
    sma_long: int
    ha_bearish_exit_count: int
    rsi_period: int
    rsi_overbought: Decimal
    rsi_oversold: Decimal
    max_spread_rate: Decimal
    price_tick: Decimal
    size_step: Decimal
    min_order_size: Decimal
    min_order_notional: Decimal
    target_order_notional: Decimal
    max_order_notional: Decimal
    min_remaining_balance: Decimal
    absolute_loss_cutoff: Decimal
    leverage: Decimal
    price_improvement_ticks: int
    time_in_force: str
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    assumed_slippage_rate: Decimal
    edge_multiplier: Decimal
    min_net_profit_rate: Decimal
    initial_virtual_jpy: Decimal
    risk_per_trade_rate: Decimal
    daily_loss_limit_rate: Decimal
    max_position_rate: Decimal
    stop_distance_rate: Decimal
    cooldown_seconds: int
    request_timeout_seconds: int
    get_retries: int
    cancel_on_exit: bool
    data_dir: Path
    log_dir: Path

    @classmethod
    def from_env(cls) -> "Config":
        dry_run = env_bool("DRY_RUN", True)
        raw_symbols = os.getenv("SYMBOLS") or os.getenv("SYMBOL", "BTC_JPY")
        symbols = tuple(item.strip().upper() for item in raw_symbols.split(",") if item.strip())
        symbol = symbols[0] if symbols else ""
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        log_dir = Path(os.getenv("LOG_DIR", "logs"))
        return cls(
            dry_run=dry_run,
            symbol=symbol,
            symbols=symbols,
            interval_seconds=env_int("LOOP_INTERVAL_SECONDS", 3600),
            kline_count=env_int("KLINE_COUNT", 100),
            kline_interval=os.getenv("KLINE_INTERVAL", "15min").strip(),
            strategy=os.getenv("STRATEGY", "EMA_RSI").strip().upper(),
            ema_short=env_int("EMA_SHORT", 9),
            ema_long=env_int("EMA_LONG", 20),
            sma_short=env_int("SMA_SHORT", 5),
            sma_long=env_int("SMA_LONG", 20),
            ha_bearish_exit_count=env_int("HA_BEARISH_EXIT_COUNT", 2),
            rsi_period=env_int("RSI_PERIOD", 14),
            rsi_overbought=env_dec("RSI_OVERBOUGHT", "70"),
            rsi_oversold=env_dec("RSI_OVERSOLD", "30"),
            max_spread_rate=env_dec("MAX_SPREAD_RATE", "0.001"),
            price_tick=env_dec("PRICE_TICK_JPY", "1"),
            size_step=env_dec("SIZE_STEP", "0.0001"),
            min_order_size=env_dec("MIN_ORDER_SIZE", "0.0001"),
            min_order_notional=env_dec("MIN_ORDER_NOTIONAL_JPY", "500"),
            target_order_notional=env_dec("TARGET_ORDER_NOTIONAL_JPY", "500"),
            max_order_notional=env_dec("MAX_ORDER_NOTIONAL_JPY", "2000"),
            min_remaining_balance=env_dec("MIN_REMAINING_BALANCE_JPY", "3000"),
            absolute_loss_cutoff=env_dec("ABSOLUTE_LOSS_CUTOFF_JPY", "50"),
            leverage=env_dec("LEVERAGE", "2"),
            price_improvement_ticks=env_int("PRICE_IMPROVEMENT_TICKS", 1),
            time_in_force=os.getenv("TIME_IN_FORCE", "FAS").upper(),
            # Match these defaults to actual account fees before live trading; initial values are conservative.
            maker_fee_rate=env_dec("MAKER_FEE_RATE", "0.0005"),
            taker_fee_rate=env_dec("TAKER_FEE_RATE", "0.0005"),
            assumed_slippage_rate=env_dec("ASSUMED_SLIPPAGE_RATE", "0.0002"),
            edge_multiplier=env_dec("EDGE_MULTIPLIER", "5"),
            min_net_profit_rate=env_dec("MIN_NET_PROFIT_RATE", "0.0001"),
            initial_virtual_jpy=env_dec("INITIAL_VIRTUAL_JPY", "1000000"),
            risk_per_trade_rate=env_dec("RISK_PER_TRADE_RATE", "0.01"),
            daily_loss_limit_rate=env_dec("DAILY_LOSS_LIMIT_RATE", "0.05"),
            max_position_rate=env_dec("MAX_POSITION_RATE", "0.10"),
            stop_distance_rate=env_dec("STOP_DISTANCE_RATE", "0.005"),
            cooldown_seconds=env_int("COOLDOWN_SECONDS", 1200),
            request_timeout_seconds=env_int("REQUEST_TIMEOUT_SECONDS", 10),
            get_retries=env_int("GET_RETRIES", 3),
            cancel_on_exit=env_bool("CANCEL_ON_EXIT", True),
            data_dir=data_dir,
            log_dir=log_dir,
        )

    @property
    def mode(self) -> str:
        return "dry-run" if self.dry_run else "live"

    @property
    def base_asset(self) -> str:
        return self.symbol.split("_")[0]

    @property
    def state_path(self) -> Path:
        return self.data_dir / f"state_{self.symbol}_{self.mode}.json"

    def validate(self) -> None:
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("SYMBOLS must be a comma-separated list of unique leverage symbols")
        if any(not re.fullmatch(r"[A-Z0-9]+_JPY", symbol) for symbol in self.symbols):
            raise ValueError("SYMBOLS must contain leverage API symbols such as BTC_JPY,ETH_JPY")
        if self.kline_interval not in {"1min", "5min", "10min", "15min", "30min", "1hour"}:
            raise ValueError("KLINE_INTERVAL must be one of 1min/5min/10min/15min/30min/1hour")
        if self.strategy not in {"EMA_RSI", "HA_SMA"}:
            raise ValueError("STRATEGY must be EMA_RSI or HA_SMA")
        if self.strategy == "EMA_RSI":
            if not (0 < self.ema_short < self.ema_long):
                raise ValueError("EMA_SHORT must be less than EMA_LONG")
            if self.rsi_period < 2 or self.kline_count < self.ema_long + 2:
                raise ValueError("KLINE_COUNT or RSI_PERIOD is insufficient")
            if not (D0 <= self.rsi_oversold < self.rsi_overbought <= D100):
                raise ValueError("Invalid RSI thresholds")
        else:
            if not (0 < self.sma_short < self.sma_long):
                raise ValueError("SMA_SHORT must be less than SMA_LONG")
            if self.kline_count < self.sma_long + 2:
                raise ValueError("KLINE_COUNT is insufficient")
            if self.ha_bearish_exit_count < 1:
                raise ValueError("HA_BEARISH_EXIT_COUNT must be at least 1")
        rates = [
            self.max_spread_rate, self.assumed_slippage_rate,
            self.risk_per_trade_rate, self.daily_loss_limit_rate,
            self.max_position_rate, self.stop_distance_rate,
        ]
        if any(x <= 0 for x in rates):
            raise ValueError("Spread, risk, and loss rates must be positive")
        if self.risk_per_trade_rate >= self.daily_loss_limit_rate:
            raise ValueError("RISK_PER_TRADE_RATE must be less than DAILY_LOSS_LIMIT_RATE")
        if self.max_position_rate > 1 or self.daily_loss_limit_rate >= 1:
            raise ValueError("Asset allocation settings are too large")
        if self.price_tick <= 0 or self.size_step <= 0 or self.min_order_size < self.size_step:
            raise ValueError("Invalid price or size step settings")
        if not (D0 < self.target_order_notional <= self.max_order_notional):
            raise ValueError("TARGET_ORDER_NOTIONAL_JPY must be positive and no greater than MAX_ORDER_NOTIONAL_JPY")
        if self.min_remaining_balance < D0 or self.leverage <= D0:
            raise ValueError("MIN_REMAINING_BALANCE_JPY must be nonnegative and LEVERAGE must be positive")
        if self.absolute_loss_cutoff <= D0:
            raise ValueError("ABSOLUTE_LOSS_CUTOFF_JPY must be positive")
        if self.interval_seconds < 60 or self.cooldown_seconds < 0:
            raise ValueError("Loop interval must be at least 60 seconds and cooldown must be nonnegative")
        if self.time_in_force not in {"FAS", "FAK", "FOK", "SOK"}:
            raise ValueError("TIME_IN_FORCE must be FAS, FAK, FOK, or SOK")
        if self.time_in_force in {"FAK", "FOK"}:
            raise ValueError("Use FAS or SOK for TIME_IN_FORCE with next-cycle cancellation")
        if self.taker_fee_rate < 0:
            raise ValueError("TAKER_FEE_RATE cannot be negative")
        if not self.dry_run:
            if not os.getenv("GMO_API_KEY") or not os.getenv("GMO_API_SECRET"):
                raise ValueError("GMO_API_KEY and GMO_API_SECRET are required in live mode")
            if os.getenv("LIVE_TRADING_ACK") != "I_UNDERSTAND_REAL_ORDERS":
                raise ValueError("LIVE_TRADING_ACK=I_UNDERSTAND_REAL_ORDERS is required in live mode")


class GMOClient:
    PUBLIC_BASE = "https://api.coin.z.com/public"
    PRIVATE_BASE = "https://api.coin.z.com/private"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api_key = os.getenv("GMO_API_KEY", "")
        self.secret = os.getenv("GMO_API_SECRET", "")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "gmo-leverage-risk-bot/2.0"})

    @staticmethod
    def signature(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
        text = timestamp + method.upper() + path + body
        return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request(
        self,
        method: str,
        visibility: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> Any:
        method = method.upper()
        base = self.PUBLIC_BASE if visibility == "public" else self.PRIVATE_BASE
        body = "" if payload is None else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers: dict[str, str] = {}
        if visibility == "private":
            timestamp = str(int(time.time() * 1000))
            # Official specification: sign only the /v1/... path, excluding queries; GET body is empty.
            sign = self.signature(self.secret, timestamp, method, path, body if method != "GET" else "")
            headers = {"API-KEY": self.api_key, "API-TIMESTAMP": timestamp, "API-SIGN": sign}
        if body:
            headers["Content-Type"] = "application/json"

        attempts = self.cfg.get_retries if method == "GET" else 1
        last_error: Optional[BaseException] = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    base + path,
                    params=params,
                    data=body if method != "GET" else None,
                    headers=headers,
                    timeout=self.cfg.request_timeout_seconds,
                )
                if response.status_code >= 500 and method == "GET" and attempt + 1 < attempts:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                if response.status_code != 200:
                    message = f"HTTP {response.status_code}: {response.text[:500]}"
                    if method != "GET" and response.status_code >= 500:
                        # Server acceptance is unknown, so the caller must reconcile by clientOrderId.
                        raise RuntimeError(f"POST outcome unknown; automatic retry is forbidden: {message}")
                    raise GMOAPIError(message)
                result = response.json()
                if result.get("status") != 0:
                    raise GMOAPIError(
                        f"GMO API error: {result.get('messages', result.get('message', result))}"
                    )
                return result.get("data")
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if method != "GET" or attempt + 1 >= attempts:
                    break
                time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"API communication failed; POST requests are not retried automatically: {last_error}")

    def status(self) -> str:
        return self._request("GET", "public", "/v1/status")["status"]

    def orderbook(self) -> dict[str, Any]:
        return self._request("GET", "public", "/v1/orderbooks", params={"symbol": self.cfg.symbol})

    def klines(self, date: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", "public", "/v1/klines",
            params={"symbol": self.cfg.symbol, "interval": self.cfg.kline_interval, "date": date},
        ) or []

    def assets(self) -> list[dict[str, Any]]:
        return self._request("GET", "private", "/v1/account/assets") or []

    def margin(self) -> dict[str, Any]:
        return self._request("GET", "private", "/v1/account/margin") or {}

    def open_positions(self) -> list[dict[str, Any]]:
        data = self._request(
            "GET", "private", "/v1/openPositions",
            params={"symbol": self.cfg.symbol, "page": 1, "count": 100},
        ) or {}
        return data.get("list", [])

    def symbol_rule(self) -> dict[str, Any]:
        rows = self._request("GET", "public", "/v1/symbols") or []
        for row in rows:
            if row.get("symbol") == self.cfg.symbol:
                return row
        raise ValueError(f"No public trading rule found for {self.cfg.symbol}")

    def get_order(self, *, order_id: str = "", client_order_id: str = "") -> Optional[dict[str, Any]]:
        params = {"orderId": order_id} if order_id else {"clientOrderId": client_order_id}
        data = self._request("GET", "private", "/v1/orders", params=params) or {}
        rows = data.get("list", [])
        return rows[0] if rows else None

    def executions_for_order(self, order_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", "private", "/v1/executions", params={"orderId": order_id}) or {}
        return data.get("list", [])

    def place_limit_order(
        self, side: str, price: Decimal, size: Decimal, client_order_id: str,
        losscut_price: Optional[Decimal] = None,
    ) -> str:
        payload = {
            "symbol": self.cfg.symbol,
            "side": side,
            "executionType": "LIMIT",
            "timeInForce": self.cfg.time_in_force,
            "price": decimal_text(price),
            "size": decimal_text(size),
            "clientOrderId": client_order_id,
        }
        if losscut_price is not None:
            payload["losscutPrice"] = decimal_text(losscut_price)
        data = self._request("POST", "private", "/v1/order", payload=payload)
        return str(data)

    def place_close_limit_order(
        self, side: str, price: Decimal, size: Decimal, position_id: str,
        client_order_id: str,
    ) -> str:
        payload = {
            "symbol": self.cfg.symbol,
            "side": side,
            "executionType": "LIMIT",
            "timeInForce": self.cfg.time_in_force,
            "price": decimal_text(price),
            "settlePosition": {"positionId": int(position_id), "size": decimal_text(size)},
            "clientOrderId": client_order_id,
        }
        data = self._request("POST", "private", "/v1/closeOrder", payload=payload)
        return str(data)

    def cancel_order(self, order_id: str) -> None:
        self._request("POST", "private", "/v1/cancelOrder", payload={"orderId": int(order_id)})


@dataclass
class BotState:
    symbol: str
    mode: str
    cash: Decimal
    account_equity: Decimal = D0
    available_balance: Decimal = D0
    position_size: Decimal = D0
    position_side: str = ""  # BUY=long position, SELL=short position
    position_id: str = ""
    position_entry_price: Decimal = D0
    position_entry_fee: Decimal = D0
    position_margin: Decimal = D0
    position_cost: Decimal = D0  # Remaining position notional plus opening fees for risk calculations.
    entry_risk_budget: Decimal = D0
    day_key: str = ""
    day_start_equity: Decimal = D0
    daily_realized_pnl: Decimal = D0
    total_realized_pnl: Decimal = D0
    daily_halted: bool = False
    pending_order: Optional[dict[str, Any]] = None
    last_order_at: Optional[str] = None
    last_fill_at: Optional[str] = None
    processed_execution_ids: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, cfg: Config) -> "BotState":
        path = cfg.state_path
        if not path.exists():
            return cls(
                symbol=cfg.symbol,
                mode=cfg.mode,
                cash=cfg.initial_virtual_jpy if cfg.dry_run else D0,
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("symbol") != cfg.symbol or raw.get("mode") != cfg.mode:
            raise ValueError(f"State file symbol or mode mismatch: {path}")
        decimal_fields = {
            "cash", "account_equity", "available_balance",
            "position_size", "position_cost", "entry_risk_budget",
            "position_entry_price", "position_entry_fee", "position_margin",
            "day_start_equity", "daily_realized_pnl", "total_realized_pnl",
        }
        for key in decimal_fields:
            if key in raw:
                raw[key] = dec(raw[key])
        raw.pop("updated_at", None)
        return cls(**raw)

    def save(self, cfg: Config) -> None:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        raw = asdict(self)
        for key, value in list(raw.items()):
            if isinstance(value, Decimal):
                raw[key] = decimal_text(value)
        raw["updated_at"] = iso_now()
        temporary = cfg.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, cfg.state_path)


class AuditLogger:
    FIELDS = [
        "timestamp_jst", "mode", "symbol", "event", "best_bid", "best_ask",
        "mid_price", "spread", "spread_rate", "ema_short", "ema_long", "rsi",
        "side", "order_price", "order_size", "order_id", "reason", "result",
        "equity_jpy", "position_size", "daily_realized_pnl", "total_realized_pnl",
    ]

    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.log_dir.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, state: BotState, **values: Any) -> None:
        path = self.cfg.log_dir / f"decisions_{datetime.now(JST):%Y%m%d}.csv"
        row = {key: "" for key in self.FIELDS}
        row.update({
            "timestamp_jst": datetime.now(JST).isoformat(timespec="milliseconds"),
            "mode": self.cfg.mode,
            "symbol": self.cfg.symbol,
            "event": event,
            "position_size": decimal_text(state.position_size),
            "daily_realized_pnl": decimal_text(state.daily_realized_pnl),
            "total_realized_pnl": decimal_text(state.total_realized_pnl),
        })
        for key, value in values.items():
            if key in row and value is not None:
                row[key] = decimal_text(value) if isinstance(value, Decimal) else str(value)
        is_new = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)


@dataclass(frozen=True)
class Book:
    best_bid: Decimal
    best_ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.best_ask - self.best_bid

    @property
    def spread_rate(self) -> Decimal:
        return self.spread / self.mid


@dataclass(frozen=True)
class Indicators:
    ema_short: Optional[Decimal]
    ema_long: Optional[Decimal]
    rsi: Optional[Decimal]
    golden_cross: bool
    dead_cross: bool
    buy_signal: bool
    sell_signal: bool
    reason: str
    strategy: str = "EMA_RSI"
    sma_short: Optional[Decimal] = None
    sma_long: Optional[Decimal] = None
    ha_open: Optional[Decimal] = None
    ha_close: Optional[Decimal] = None
    ha_bearish_count: int = 0

    @property
    def trend_short(self) -> Decimal:
        value = self.sma_short if self.strategy == "HA_SMA" else self.ema_short
        if value is None:
            raise ValueError("Short-term trend indicator has not been calculated")
        return value

    @property
    def trend_long(self) -> Decimal:
        value = self.sma_long if self.strategy == "HA_SMA" else self.ema_long
        if value is None:
            raise ValueError("Long-term trend indicator has not been calculated")
        return value


def indicator_log_values(indicators: Indicators) -> dict[str, Any]:
    return {
        "strategy": indicators.strategy,
        "ema_short": indicators.ema_short,
        "ema_long": indicators.ema_long,
        "rsi": indicators.rsi,
        "sma_short": indicators.sma_short,
        "sma_long": indicators.sma_long,
        "ha_open": indicators.ha_open,
        "ha_close": indicators.ha_close,
        "ha_bearish_count": indicators.ha_bearish_count,
    }


def parse_book(raw: dict[str, Any]) -> Book:
    bids = raw.get("bids") or []
    asks = raw.get("asks") or []
    if not bids or not asks:
        raise ValueError("Order book is empty")
    best_bid = max(dec(row["price"]) for row in bids)
    best_ask = min(dec(row["price"]) for row in asks)
    if best_bid <= 0 or best_ask <= best_bid:
        raise ValueError(f"Invalid order book: bid={best_bid}, ask={best_ask}")
    return Book(best_bid=best_bid, best_ask=best_ask)


def ema_series(values: Iterable[Decimal], period: int) -> list[Decimal]:
    rows = list(values)
    if len(rows) < period:
        return []
    alpha = Decimal("2") / Decimal(period + 1)
    current = sum(rows[:period]) / Decimal(period)
    result = [current]
    for value in rows[period:]:
        current = value * alpha + current * (D1 - alpha)
        result.append(current)
    return result


def sma_series(values: Iterable[Decimal], period: int) -> list[Decimal]:
    rows = list(values)
    if period < 1 or len(rows) < period:
        return []
    divisor = Decimal(period)
    return [
        sum(rows[index - period + 1:index + 1]) / divisor
        for index in range(period - 1, len(rows))
    ]


def heikin_ashi(candles: list[dict[str, Decimal]]) -> list[dict[str, Decimal]]:
    result: list[dict[str, Decimal]] = []
    previous_open: Optional[Decimal] = None
    previous_close: Optional[Decimal] = None
    four = Decimal("4")
    for candle in candles:
        ha_close = (
            candle["open"] + candle["high"] + candle["low"] + candle["close"]
        ) / four
        if previous_open is None or previous_close is None:
            ha_open = (candle["open"] + candle["close"]) / 2
        else:
            ha_open = (previous_open + previous_close) / 2
        result.append({
            "open": ha_open,
            "high": max(candle["high"], ha_open, ha_close),
            "low": min(candle["low"], ha_open, ha_close),
            "close": ha_close,
        })
        previous_open = ha_open
        previous_close = ha_close
    return result


def rsi_wilder(values: Iterable[Decimal], period: int) -> Optional[Decimal]:
    rows = list(values)
    if len(rows) <= period:
        return None
    changes = [rows[i] - rows[i - 1] for i in range(1, len(rows))]
    gains = [max(x, D0) for x in changes]
    losses = [max(-x, D0) for x in changes]
    avg_gain = sum(gains[:period]) / Decimal(period)
    avg_loss = sum(losses[:period]) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return D100 if avg_gain > 0 else Decimal("50")
    rs = avg_gain / avg_loss
    return D100 - D100 / (D1 + rs)


def evaluate_indicators(closes: list[Decimal], cfg: Config) -> Indicators:
    short_series = ema_series(closes, cfg.ema_short)
    long_series = ema_series(closes, cfg.ema_long)
    rsi = rsi_wilder(closes, cfg.rsi_period)
    if len(short_series) < 2 or len(long_series) < 2 or rsi is None:
        raise ValueError("Not enough closed candles to calculate indicators")
    # Align the short EMA series with the end of the long EMA series.
    short_now = short_series[-1]
    short_prev = short_series[-2]
    long_now = long_series[-1]
    long_prev = long_series[-2]
    golden = short_prev <= long_prev and short_now > long_now
    dead = short_prev >= long_prev and short_now < long_now
    buy = golden or (short_now > long_now and rsi <= cfg.rsi_oversold)
    sell = dead or rsi >= cfg.rsi_overbought
    reason = (
        f"EMA_short={short_now:.2f}, EMA_long={long_now:.2f}, RSI={rsi:.2f}, "
        f"GC={golden}, DC={dead}, RSI_oversold={rsi <= cfg.rsi_oversold}, "
        f"RSI_overbought={rsi >= cfg.rsi_overbought}"
    )
    return Indicators(short_now, long_now, rsi, golden, dead, buy, sell, reason)


def evaluate_ha_sma(candles: list[dict[str, Decimal]], cfg: Config) -> Indicators:
    closes = [candle["close"] for candle in candles]
    short_series = sma_series(closes, cfg.sma_short)
    long_series = sma_series(closes, cfg.sma_long)
    ha_candles = heikin_ashi(candles)
    if len(short_series) < 2 or len(long_series) < 2 or not ha_candles:
        raise ValueError("Not enough closed candles to calculate Heikin-Ashi and SMA indicators")
    short_now = short_series[-1]
    short_prev = short_series[-2]
    long_now = long_series[-1]
    long_prev = long_series[-2]
    golden = short_prev <= long_prev and short_now > long_now
    dead = short_prev >= long_prev and short_now < long_now
    latest_ha = ha_candles[-1]
    bullish = latest_ha["close"] > latest_ha["open"]
    bearish_count = 0
    for ha_candle in reversed(ha_candles):
        if ha_candle["close"] >= ha_candle["open"]:
            break
        bearish_count += 1
    early_exit = bearish_count >= cfg.ha_bearish_exit_count
    buy = golden and bullish
    sell = dead or early_exit
    reason = (
        f"SMA_short={short_now:.2f}, SMA_long={long_now:.2f}, "
        f"HA_open={latest_ha['open']:.2f}, HA_close={latest_ha['close']:.2f}, "
        f"GC={golden}, DC={dead}, HA_bullish={bullish}, "
        f"consecutive_bearish={bearish_count}, bearish_early_exit={early_exit}"
    )
    return Indicators(
        ema_short=None, ema_long=None, rsi=None,
        golden_cross=golden, dead_cross=dead, buy_signal=buy, sell_signal=sell,
        reason=reason, strategy="HA_SMA", sma_short=short_now, sma_long=long_now,
        ha_open=latest_ha["open"], ha_close=latest_ha["close"],
        ha_bearish_count=bearish_count,
    )


def evaluate_strategy(candles: list[dict[str, Decimal]], cfg: Config) -> Indicators:
    if cfg.strategy == "HA_SMA":
        return evaluate_ha_sma(candles, cfg)
    return evaluate_indicators([candle["close"] for candle in candles], cfg)


def gmo_kline_day(now: datetime) -> datetime.date:
    # The one-minute candle date changes at 06:00 JST.
    return (now.astimezone(JST) - timedelta(hours=6)).date()


KLINE_INTERVAL_MS = {
    "1min": 60_000,
    "5min": 300_000,
    "10min": 600_000,
    "15min": 900_000,
    "30min": 1_800_000,
    "1hour": 3_600_000,
}


def extract_closed_closes(
    current_day: list[dict[str, Any]], previous_day: list[dict[str, Any]], now: datetime,
    interval: str = "1min",
) -> list[Decimal]:
    unique: dict[int, dict[str, Any]] = {}
    for row in previous_day + current_day:
        unique[int(row["openTime"])] = row
    now_ms = int(now.timestamp() * 1000)
    return [
        dec(unique[key]["close"])
        for key in sorted(unique)
        if key + KLINE_INTERVAL_MS[interval] <= now_ms
    ]


def extract_closed_candles(
    current_day: list[dict[str, Any]], previous_day: list[dict[str, Any]], now: datetime,
    interval: str = "1min",
) -> list[dict[str, Decimal]]:
    unique: dict[int, dict[str, Any]] = {}
    for row in previous_day + current_day:
        unique[int(row["openTime"])] = row
    now_ms = int(now.timestamp() * 1000)
    result: list[dict[str, Decimal]] = []
    for key in sorted(unique):
        if key + KLINE_INTERVAL_MS[interval] > now_ms:
            continue
        row = unique[key]
        result.append({
            "open": dec(row["open"]),
            "high": dec(row["high"]),
            "low": dec(row["low"]),
            "close": dec(row["close"]),
        })
    return result


def improved_limit_price(side: str, book: Book, cfg: Config, stop_loss: bool = False) -> Decimal:
    tick = cfg.price_tick
    improvement = tick * cfg.price_improvement_ticks
    if side == "BUY":
        if stop_loss:
            # For short stop-losses, use a best_ask limit instead of a market order.
            return ceil_step(book.best_ask, tick)
        raw = min(book.best_bid + improvement, book.best_ask - tick)
        price = floor_step(raw, tick)
        if not (book.best_bid <= price < book.best_ask):
            raise ValueError("Invalid calculated buy limit price")
        return price
    if stop_loss:
        # For stop-losses, use a best_bid limit instead of a market order.
        return floor_step(book.best_bid, tick)
    raw = max(book.best_ask - improvement, book.best_bid + tick)
    price = ceil_step(raw, tick)
    if not (book.best_bid < price <= book.best_ask):
        raise ValueError("Invalid calculated sell limit price")
    return price


def round_trip_cost_rate(cfg: Config) -> Decimal:
    # Limit orders may still be takers, so conservatively assume taker fees plus slippage both ways.
    return Decimal("2") * (cfg.taker_fee_rate + cfg.assumed_slippage_rate)


def fee_gate(indicators: Indicators, mid: Decimal, cfg: Config) -> tuple[bool, str]:
    expected_gross = abs(indicators.trend_short - indicators.trend_long) / mid * cfg.edge_multiplier
    maker_cost = Decimal("2") * cfg.maker_fee_rate + Decimal("2") * cfg.assumed_slippage_rate
    cost = round_trip_cost_rate(cfg)
    expected_net = expected_gross - cost
    passed = expected_net >= cfg.min_net_profit_rate
    reason = (
        f"expected_gross={expected_gross:.6%}, maker_round_trip_cost={maker_cost:.6%}, "
        f"conservative_taker_round_trip_cost={cost:.6%}, expected_net={expected_net:.6%}, "
        f"required_net={cfg.min_net_profit_rate:.6%}"
    )
    return passed, reason


def calculate_order_size(
    equity: Decimal, available_jpy: Decimal, mid: Decimal, cfg: Config
) -> tuple[Decimal, Decimal, str]:
    """Target about 500 JPY and skip symbols whose minimum order costs at least 2,000 JPY."""
    risk_budget = equity * cfg.risk_per_trade_rate
    risk_per_unit = mid * (cfg.stop_distance_rate + round_trip_cost_rate(cfg))
    by_risk = risk_budget / risk_per_unit
    by_allocation = equity * cfg.max_position_rate / mid
    usable = max(D0, available_jpy - cfg.min_remaining_balance)
    by_balance = usable * cfg.leverage / mid
    raw_size = min(by_risk, by_allocation, by_balance)
    minimum_notional = cfg.min_order_size * mid
    if minimum_notional >= cfg.max_order_notional:
        return D0, risk_budget, (
            f"minimum_notional={minimum_notional:.2f} JPY >= "
            f"{cfg.max_order_notional} JPY; skipping this symbol"
        )
    target_size = floor_step(cfg.target_order_notional / mid, cfg.size_step)
    size = max(cfg.min_order_size, target_size)
    size = min(size, floor_step(raw_size, cfg.size_step))
    notional = size * mid
    required = notional / cfg.leverage + notional * cfg.taker_fee_rate
    remaining = available_jpy - required
    reason = (
        f"target={cfg.target_order_notional} JPY, min_size={cfg.min_order_size}, "
        f"size={size}, notional={notional:.2f} JPY, required_margin_and_fee={required:.2f} JPY, "
        f"remaining={remaining:.2f} JPY, by_risk={by_risk}, by_allocation={by_allocation}"
    )
    if (
        size < cfg.min_order_size
        or notional > cfg.max_order_notional
        or remaining < cfg.min_remaining_balance
    ):
        return D0, risk_budget, reason + ", size, notional cap, or available balance constraints not met"
    return size, risk_budget, reason


class TradingBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = GMOClient(cfg)
        self.state = BotState.load(cfg)
        self.audit = AuditLogger(cfg)

    def unrealized_pnl(self, mid: Decimal) -> Decimal:
        if self.state.position_size <= 0 or not self.state.position_side:
            return D0
        direction = D1 if self.state.position_side == "BUY" else Decimal("-1")
        return (mid - self.state.position_entry_price) * self.state.position_size * direction

    def estimated_close_pnl(self, book: Book) -> Decimal:
        """Estimate current PnL using the closing-side quote and opening/closing fees."""
        if self.state.position_size <= 0:
            return D0
        direction = D1 if self.state.position_side == "BUY" else Decimal("-1")
        exit_price = book.best_bid if self.state.position_side == "BUY" else book.best_ask
        gross = (exit_price - self.state.position_entry_price) * self.state.position_size * direction
        exit_fee = exit_price * self.state.position_size * self.cfg.taker_fee_rate
        return gross - self.state.position_entry_fee - exit_fee

    def favorable_trend(self, indicators: Indicators) -> bool:
        """Let profits run for long positions in uptrends and short positions in downtrends."""
        if self.state.position_size <= 0:
            return False
        if self.state.position_side == "BUY":
            return indicators.trend_short > indicators.trend_long
        return indicators.trend_short < indicators.trend_long

    def account_snapshot(self, mid: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        """Return account equity, available leverage balance, and an unused compatibility value."""
        if self.cfg.dry_run:
            equity = self.state.cash + self.unrealized_pnl(mid)
            available = max(D0, self.state.cash - self.state.position_margin)
            self.state.account_equity = equity
            self.state.available_balance = available
            return equity, available, D0
        margin = self.client.margin()
        if margin.get("marginCallStatus") not in {None, "NORMAL"}:
            raise RuntimeError(f"Abnormal margin status: {margin.get('marginCallStatus')}")
        if margin.get("cryptoShortfallStatus") not in {None, "NORMAL"}:
            raise RuntimeError(f"Abnormal crypto collateral status: {margin.get('cryptoShortfallStatus')}")
        equity = dec(margin["actualProfitLoss"])
        available = dec(margin["availableAmount"])
        self.state.account_equity = equity
        self.state.available_balance = available
        return equity, available, D0

    def roll_day(self, equity: Decimal, now: datetime) -> None:
        day = now.astimezone(JST).date().isoformat()
        if self.state.day_key != day:
            self.state.day_key = day
            self.state.day_start_equity = equity
            self.state.daily_realized_pnl = D0
            self.state.daily_halted = False
            LOGGER.info("Resetting daily risk state: %s, equity=%s", day, equity)
        elif self.state.day_start_equity <= 0:
            self.state.day_start_equity = equity

    def daily_loss_exceeded(self, equity: Decimal) -> bool:
        if self.state.day_start_equity <= 0:
            return False
        loss = self.state.day_start_equity - equity
        limit = self.state.day_start_equity * self.cfg.daily_loss_limit_rate
        if loss >= limit:
            if not self.state.daily_halted:
                LOGGER.error("Daily loss limit reached: loss=%s, limit=%s", loss, limit)
            self.state.daily_halted = True
        return self.state.daily_halted

    def apply_fill(
        self,
        *,
        side: str,
        price: Decimal,
        size: Decimal,
        fee: Decimal,
        execution_id: str,
        book: Book,
        indicators: Indicators,
        settle_type: str = "OPEN",
        position_id: str = "",
        loss_gain: Optional[Decimal] = None,
    ) -> Decimal:
        if size <= 0:
            return D0
        pending = self.state.pending_order or {}
        settle_type = settle_type or pending.get("settle_type", "OPEN")
        realized = D0
        if settle_type == "OPEN":
            if self.state.position_size > 0:
                raise RuntimeError("Unexpected additional position fill for the same symbol; manual review required")
            self.state.position_side = side
            self.state.position_id = position_id or pending.get("position_id", "")
            self.state.position_size = size
            self.state.position_entry_price = price
            self.state.position_entry_fee = fee
            self.state.position_cost = price * size + fee
            self.state.position_margin = price * size / self.cfg.leverage
            self.state.entry_risk_budget = dec(pending.get("risk_budget", "0"))
            if self.cfg.dry_run:
                self.state.cash -= fee
        elif settle_type == "CLOSE":
            expected_side = "SELL" if self.state.position_side == "BUY" else "BUY"
            if self.state.position_size <= 0 or side != expected_side:
                raise RuntimeError("Close fill does not match the position side; manual review required")
            if size > self.state.position_size + self.cfg.size_step / 1000:
                raise RuntimeError("Close fill exceeds the bot-managed position; manual review required")
            size = min(size, self.state.position_size)
            before = self.state.position_size
            ratio = size / before
            entry_fee = self.state.position_entry_fee * ratio
            direction = D1 if self.state.position_side == "BUY" else Decimal("-1")
            gross = (price - self.state.position_entry_price) * size * direction
            realized = gross - fee - entry_fee
            if loss_gain is not None and not self.cfg.dry_run:
                # GMO lossGain excludes closing fees, so subtract only the closing fee.
                realized = loss_gain - fee
            self.state.daily_realized_pnl += realized
            self.state.total_realized_pnl += realized
            if self.cfg.dry_run:
                # Opening fees were deducted on OPEN, so apply only gross PnL and closing fees here.
                self.state.cash += gross - fee
            self.state.position_size -= size
            self.state.position_cost -= self.state.position_cost * ratio
            self.state.position_entry_fee -= entry_fee
            self.state.position_margin -= self.state.position_margin * ratio
            if self.state.position_size <= self.cfg.size_step / 1000:
                self.state.position_size = D0
                self.state.position_side = ""
                self.state.position_id = ""
                self.state.position_entry_price = D0
                self.state.position_entry_fee = D0
                self.state.position_margin = D0
                self.state.position_cost = D0
                self.state.entry_risk_budget = D0
        else:
            raise ValueError(f"Unknown settleType: {settle_type}")
        self.state.last_fill_at = iso_now()
        if execution_id:
            self.state.processed_execution_ids.append(execution_id)
            self.state.processed_execution_ids = self.state.processed_execution_ids[-2000:]
        if pending:
            applied = dec(pending.get("applied_size", "0")) + size
            pending["applied_size"] = decimal_text(applied)
        self.audit.write(
            "FILL", self.state, side=side, order_price=price, order_size=size,
            order_id=(pending or {}).get("order_id", ""),
            reason=(pending or {}).get("reason", ""),
            result=f"settle={settle_type}, position_side={self.state.position_side}, fee={fee}, realized={realized}",
            best_bid=book.best_bid, best_ask=book.best_ask, mid_price=book.mid,
            spread=book.spread, spread_rate=book.spread_rate,
            **indicator_log_values(indicators),
        )
        LOGGER.info("Fill: %s %s size=%s price=%s fee=%s realized=%s", settle_type, side, size, price, fee, realized)
        return realized

    def reconcile_dry_order(self, book: Book, indicators: Indicators) -> None:
        pending = self.state.pending_order
        if not pending:
            return
        side = pending["side"]
        limit = dec(pending["price"])
        size = dec(pending["size"])
        filled = (side == "BUY" and book.best_ask <= limit) or (
            side == "SELL" and book.best_bid >= limit
        )
        if filled:
            # Conservatively simulate fills at the limit price and apply taker fees.
            fill_price = limit
            fee = fill_price * size * self.cfg.taker_fee_rate
            self.apply_fill(
                side=side, price=fill_price, size=size, fee=fee,
                execution_id="", book=book, indicators=indicators,
                settle_type=pending.get("settle_type", "OPEN"),
                position_id=pending.get("position_id", ""),
            )
            result = "dry-run filled in full (conservative simplified fill model)"
        else:
            result = "Unfilled; dry-run order canceled"
        self.audit.write(
            "ORDER_CLOSE", self.state, side=side, order_price=limit, order_size=size,
            order_id=pending["order_id"], reason=pending["reason"], result=result,
            best_bid=book.best_bid, best_ask=book.best_ask, mid_price=book.mid,
            spread=book.spread, spread_rate=book.spread_rate,
            **indicator_log_values(indicators),
        )
        self.state.pending_order = None
        self.state.save(self.cfg)

    def reconcile_live_order(self, book: Book, indicators: Indicators) -> None:
        pending = self.state.pending_order
        if not pending:
            return
        order = self.client.get_order(
            order_id=pending.get("order_id", ""),
            client_order_id=pending["client_order_id"],
        )
        if order is None:
            attempts = int(pending.get("reconcile_attempts", 0)) + 1
            pending["reconcile_attempts"] = attempts
            self.audit.write(
                "ORDER_UNKNOWN", self.state, order_id=pending.get("order_id", ""),
                reason="Reconciliation after a communication failure", result=f"Order not found ({attempts}/3)",
            )
            if attempts >= 3:
                # Treat the order as rejected only after three clientOrderId lookups find nothing.
                self.state.pending_order = None
                LOGGER.warning("Clearing pending order because it does not exist in the API: %s", pending["client_order_id"])
            self.state.save(self.cfg)
            return

        order_id = str(order["orderId"])
        pending["order_id"] = order_id
        executions = self.client.executions_for_order(order_id)
        executions.sort(key=lambda row: row.get("timestamp", ""))
        known = set(self.state.processed_execution_ids)
        for execution in executions:
            execution_id = str(execution["executionId"])
            if execution_id in known:
                continue
            self.apply_fill(
                side=execution["side"], price=dec(execution["price"]),
                size=dec(execution["size"]), fee=dec(execution.get("fee", "0")),
                execution_id=execution_id, book=book, indicators=indicators,
                settle_type=execution.get("settleType", ""),
                position_id=str(execution.get("positionId", "")),
                loss_gain=dec(execution["lossGain"]) if execution.get("lossGain") not in {None, ""} else None,
            )
            known.add(execution_id)

        status = order.get("status", "")
        executed_size = dec(order.get("executedSize", "0"))
        applied_size = dec(pending.get("applied_size", "0"))
        self.audit.write(
            "ORDER_STATUS", self.state, side=pending["side"],
            order_price=dec(pending["price"]), order_size=dec(pending["size"]),
            order_id=order_id, reason=pending["reason"],
            result=f"status={status}, executed={executed_size}, applied={applied_size}",
            best_bid=book.best_bid, best_ask=book.best_ask, mid_price=book.mid,
            spread=book.spread, spread_rate=book.spread_rate,
            **indicator_log_values(indicators),
        )
        if status in FINAL_ORDER_STATUSES:
            if executed_size > applied_size + self.cfg.size_step / 1000:
                LOGGER.warning("Keeping pending order because executions have not been fully reconciled: %s", order_id)
            else:
                self.state.pending_order = None
        elif status in ACTIVE_ORDER_STATUSES and not pending.get("cancel_requested"):
            self.client.cancel_order(order_id)
            pending["cancel_requested"] = True
            self.audit.write(
                "CANCEL_REQUEST", self.state, order_id=order_id,
                reason="Canceling the unfilled remainder on the next cycle", result="accepted",
            )
        self.state.save(self.cfg)

    def reconcile(self, book: Book, indicators: Indicators) -> None:
        if self.cfg.dry_run:
            self.reconcile_dry_order(book, indicators)
        else:
            self.reconcile_live_order(book, indicators)

    def submit_order(
        self,
        *,
        side: str,
        price: Decimal,
        size: Decimal,
        reason: str,
        risk_budget: Decimal,
        book: Book,
        indicators: Indicators,
        equity: Decimal,
        available_jpy: Decimal,
        available_base: Decimal = D0,
        settle_type: str = "OPEN",
        position_id: str = "",
    ) -> None:
        if self.state.pending_order:
            raise RuntimeError("Cannot submit a new order while another order is pending")
        if settle_type == "OPEN" and self.state.position_size > 0:
            raise RuntimeError(f"Additional positions are forbidden while {self.cfg.symbol} has an open position")
        if settle_type == "CLOSE" and (
            self.state.position_size < size or not position_id
        ):
            raise RuntimeError("Invalid position ID or size for the close order")
        if settle_type == "OPEN":
            notional = price * size
            required = notional / self.cfg.leverage + notional * self.cfg.taker_fee_rate
            remaining = available_jpy - required
            if remaining < self.cfg.min_remaining_balance:
                raise ValueError(
                    f"Available balance after the order would be below the minimum: required={required}, "
                    f"remaining={remaining}, minimum={self.cfg.min_remaining_balance}"
                )
        client_order_id = uuid.uuid4().hex
        pending = {
            "order_id": f"dry-{client_order_id}" if self.cfg.dry_run else "",
            "client_order_id": client_order_id,
            "side": side,
            "settle_type": settle_type,
            "position_id": position_id,
            "price": decimal_text(price),
            "size": decimal_text(size),
            "reason": reason,
            "risk_budget": decimal_text(risk_budget),
            "placed_at": iso_now(),
            "applied_size": "0",
            "cancel_requested": False,
            "reconcile_attempts": 0,
        }
        # Save clientOrderId before live submission to prevent automatic resubmission after failures.
        self.state.pending_order = pending
        self.state.last_order_at = iso_now()
        self.state.save(self.cfg)
        try:
            if not self.cfg.dry_run:
                if settle_type == "OPEN":
                    losscut = price * (
                        D1 - self.cfg.stop_distance_rate if side == "BUY"
                        else D1 + self.cfg.stop_distance_rate
                    )
                    losscut = floor_step(losscut, self.cfg.price_tick)
                    pending["order_id"] = self.client.place_limit_order(
                        side, price, size, client_order_id, losscut
                    )
                else:
                    pending["order_id"] = self.client.place_close_limit_order(
                        side, price, size, position_id, client_order_id
                    )
                self.state.save(self.cfg)
            result = "accepted (simulated)" if self.cfg.dry_run else f"accepted orderId={pending['order_id']}"
        except GMOAPIError:
            # Clear pending only after explicit rejection; retain timeouts and other unknown outcomes for reconciliation.
            self.state.pending_order = None
            self.state.save(self.cfg)
            raise
        self.audit.write(
            "ORDER", self.state, side=side, order_price=price, order_size=size,
            order_id=pending["order_id"], reason=reason, result=f"{settle_type}: {result}",
            best_bid=book.best_bid, best_ask=book.best_ask, mid_price=book.mid,
            spread=book.spread, spread_rate=book.spread_rate,
            equity_jpy=equity, **indicator_log_values(indicators),
        )
        LOGGER.info("Limit order: %s %s price=%s size=%s %s", settle_type, side, price, size, result)

    def candles(self, now: datetime) -> list[dict[str, Decimal]]:
        day = gmo_kline_day(now)
        per_day = 86_400_000 // KLINE_INTERVAL_MS[self.cfg.kline_interval]
        days_needed = max(2, (self.cfg.kline_count + per_day - 1) // per_day + 1)
        all_candles: list[dict[str, Decimal]] = []
        for offset in range(days_needed - 1, -1, -1):
            rows = self.client.klines((day - timedelta(days=offset)).strftime("%Y%m%d"))
            closed = extract_closed_candles(rows, [], now, self.cfg.kline_interval)
            all_candles.extend(closed)
        # Dates do not overlap, so return the last K closed candles.
        return all_candles[-self.cfg.kline_count:]

    def run_once(self) -> None:
        now = utc_now()
        exchange_status = self.client.status()
        if exchange_status != "OPEN":
            self.audit.write("SKIP", self.state, reason="Exchange status is not OPEN", result=exchange_status)
            LOGGER.warning("Exchange status: %s", exchange_status)
            return
        # Prefer public trading rules over fixed .env values for minimum size, steps, and fees.
        rule = self.client.symbol_rule()
        self.cfg = replace(
            self.cfg,
            price_tick=dec(rule["tickSize"]), size_step=dec(rule["sizeStep"]),
            min_order_size=dec(rule["minOrderSize"]),
            maker_fee_rate=dec(rule.get("makerFee", self.cfg.maker_fee_rate)),
            taker_fee_rate=dec(rule.get("takerFee", self.cfg.taker_fee_rate)),
        )
        book = parse_book(self.client.orderbook())
        candles = self.candles(now)
        indicators = evaluate_strategy(candles, self.cfg)
        equity, available_jpy, available_base = self.account_snapshot(book.mid)
        self.roll_day(equity, now)

        common = dict(
            best_bid=book.best_bid, best_ask=book.best_ask, mid_price=book.mid,
            spread=book.spread, spread_rate=book.spread_rate, equity_jpy=equity,
            **indicator_log_values(indicators),
        )
        self.audit.write("MARKET", self.state, reason=indicators.reason, result="evaluated", **common)

        # Reconcile fills and cancellations from the previous cycle before making new decisions.
        self.reconcile(book, indicators)
        if not self.cfg.dry_run:
            positions = self.client.open_positions()
            if positions and self.state.position_size <= 0:
                self.audit.write(
                    "SKIP", self.state, reason="Existing positions include positions not managed by this bot",
                    result=f"count={len(positions)}; New and additional orders are forbidden", **common,
                )
                return
            if positions and self.state.position_size > 0:
                managed = next(
                    (row for row in positions if str(row.get("positionId")) == self.state.position_id),
                    None,
                )
                if managed is None or len(positions) != 1:
                    self.audit.write(
                        "SKIP", self.state, reason="Position ID mismatch or multiple positions detected",
                        result="Orders stopped until positions are reviewed manually", **common,
                    )
                    return
                self.state.position_size = dec(managed["size"])
                self.state.position_side = managed["side"]
                self.state.position_entry_price = dec(managed["price"])
            if not positions and self.state.position_size > 0 and not self.state.pending_order:
                LOGGER.warning("Position was closed externally or liquidated: %s", self.cfg.symbol)
                self.audit.write("POSITION_SYNC", self.state, reason="No position found in the API", result="Clearing the local position", **common)
                self.state.position_size = D0
                self.state.position_side = ""
                self.state.position_id = ""
                self.state.position_entry_price = D0
                self.state.position_entry_fee = D0
                self.state.position_margin = D0
                self.state.position_cost = D0
                self.state.entry_risk_budget = D0
        equity, available_jpy, available_base = self.account_snapshot(book.mid)
        self.roll_day(equity, now)
        common["equity_jpy"] = equity
        if self.state.pending_order:
            self.state.save(self.cfg)
            return

        # Close at -50 JPY or less before all other filters, without market orders.
        # Sell long positions at best_bid and buy back short positions at best_ask.
        forced_loss = (
            self.state.position_size > 0
            and self.estimated_close_pnl(book) <= -self.cfg.absolute_loss_cutoff
        )
        if forced_loss:
            side = "SELL" if self.state.position_side == "BUY" else "BUY"
            size = floor_step(self.state.position_size, self.cfg.size_step)
            if size < self.cfg.min_order_size:
                raise RuntimeError("Forced close size is below the minimum order size; review immediately")
            estimated_net = self.estimated_close_pnl(book)
            price = improved_limit_price(side, book, self.cfg, stop_loss=True)
            reason = (
                f"FORCED_LOSS_CUTOFF: estimated_net_pnl={estimated_net:.2f} JPY <= "
                f"-{self.cfg.absolute_loss_cutoff} JPY; bypassing spread, cooldown, and daily halt"
            )
            self.submit_order(
                side=side, price=price, size=size, reason=reason, risk_budget=D0,
                book=book, indicators=indicators, equity=equity,
                available_jpy=available_jpy, available_base=available_base,
                settle_type="CLOSE", position_id=self.state.position_id or f"dry-{self.cfg.symbol}",
            )
            return

        if self.daily_loss_exceeded(equity):
            self.state.save(self.cfg)
            self.audit.write(
                "DAILY_HALT", self.state, reason="Daily loss limit reached; all actions except the absolute-loss forced close are stopped",
                result=f"start={self.state.day_start_equity}, equity={equity}", **common,
            )
            return
        if book.spread_rate >= self.cfg.max_spread_rate:
            self.audit.write(
                "SKIP", self.state, reason="Spread limit exceeded",
                result=f"{book.spread_rate:.6%} >= {self.cfg.max_spread_rate:.6%}", **common,
            )
            return

        stop_loss = False
        stop_reason = ""
        if self.state.position_size > 0:
            estimated_net = self.estimated_close_pnl(book)
            estimated_loss = max(D0, -estimated_net)
            budget = self.state.entry_risk_budget or equity * self.cfg.risk_per_trade_rate
            stop_loss = estimated_loss >= budget
            stop_reason = f"estimated_net_loss={estimated_loss:.2f} JPY, loss_budget={budget:.2f} JPY"

        last_order = parse_time(self.state.last_order_at)
        cooling = (
            last_order is not None
            and (now - last_order).total_seconds() < self.cfg.cooldown_seconds
        )
        if cooling and not stop_loss:
            self.audit.write("SKIP", self.state, reason="Cooldown active", result=self.state.last_order_at, **common)
            return

        if self.state.position_size > 0:
            profitable = estimated_net > D0
            favorable = self.favorable_trend(indicators)
            exit_signal = indicators.sell_signal if self.state.position_side == "BUY" else indicators.buy_signal
            if profitable and favorable and not stop_loss:
                self.audit.write(
                    "HOLD", self.state, reason="Holding a profitable position while the trend remains favorable",
                    result=f"Keeping {self.state.position_side} position; estimated_net_pnl={estimated_net:.2f} JPY", **common,
                )
                return
            if stop_loss or exit_signal:
                side = "SELL" if self.state.position_side == "BUY" else "BUY"
                size = floor_step(self.state.position_size, self.cfg.size_step)
                reason = ("STOP_LOSS: " if stop_loss else "EXIT: ") + stop_reason + indicators.reason
                if size < self.cfg.min_order_size:
                    self.audit.write("SKIP", self.state, reason=reason, result="Close size is below the minimum order size", **common)
                    return
                price = improved_limit_price(side, book, self.cfg, stop_loss=stop_loss)
                self.submit_order(
                    side=side, price=price, size=size, reason=reason, risk_budget=D0,
                    book=book, indicators=indicators, equity=equity,
                    available_jpy=available_jpy, available_base=available_base,
                    settle_type="CLOSE", position_id=self.state.position_id or f"dry-{self.cfg.symbol}",
                )
                return
            self.audit.write("HOLD", self.state, reason=indicators.reason, result=f"Keeping {self.state.position_side} position", **common)
            return

        entry_side = "BUY" if indicators.buy_signal else "SELL" if indicators.sell_signal else ""
        if entry_side:
            passed, fee_reason = fee_gate(indicators, book.mid, self.cfg)
            if not passed:
                self.audit.write("SKIP", self.state, reason="Net profit filter", result=fee_reason, **common)
                return
            size, risk_budget, size_reason = calculate_order_size(
                equity, available_jpy, book.mid, self.cfg
            )
            if size <= 0:
                self.audit.write(
                    "SKIP", self.state, reason="Minimum size, notional cap, and post-order balance filter",
                    result=size_reason, **common,
                )
                return
            price = improved_limit_price(entry_side, book, self.cfg)
            reason = f"ENTRY {entry_side}: " + indicators.reason + "; " + fee_reason + "; " + size_reason
            self.submit_order(
                side=entry_side, price=price, size=size, reason=reason, risk_budget=risk_budget,
                book=book, indicators=indicators, equity=equity,
                available_jpy=available_jpy, available_base=available_base,
            )
            return

        self.audit.write("HOLD", self.state, reason=indicators.reason, result="No open position", **common)

    def cancel_pending_on_exit(self) -> None:
        pending = self.state.pending_order
        if self.cfg.dry_run or not pending or not pending.get("order_id"):
            return
        try:
            self.client.cancel_order(pending["order_id"])
            pending["cancel_requested"] = True
            self.state.save(self.cfg)
            LOGGER.info("Requested cancellation of the unfilled order on exit: %s", pending["order_id"])
        except Exception as exc:
            LOGGER.error("Cancellation on exit failed; review the order manually: %s", exc)


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        cfg.log_dir / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(console)
    LOGGER.addHandler(file_handler)


class SingleInstance:
    """Prevent duplicate processes using the same state file on POSIX; create only elsewhere."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        except BlockingIOError as exc:
            raise RuntimeError(f"A bot with the same configuration is already running: {self.path}") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle:
            try:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass
            self.handle.close()


def run_loop(cfg: Config, once: bool) -> None:
    bots = [TradingBot(replace(cfg, symbol=symbol)) for symbol in cfg.symbols]
    stop_event = threading.Event()

    def request_stop(signum: int, _: Any) -> None:
        LOGGER.info("Stop signal received: %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    LOGGER.info(
        "Started: mode=%s symbols=%s interval=%ss states=%s",
        cfg.mode, ",".join(cfg.symbols), cfg.interval_seconds,
        ",".join(str(bot.cfg.state_path) for bot in bots),
    )
    try:
        while not stop_event.is_set():
            started = time.monotonic()
            for bot in bots:
                if stop_event.is_set():
                    break
                try:
                    bot.run_once()
                except Exception:
                    # One symbol failure must not block the other symbols.
                    LOGGER.exception("Cycle error for %s; continuing with other symbols", bot.cfg.symbol)
                    bot.audit.write(
                        "ERROR", bot.state, reason="Cycle exception", result="See bot log for details"
                    )
            if once:
                break
            elapsed = time.monotonic() - started
            stop_event.wait(max(1.0, cfg.interval_seconds - elapsed))
    finally:
        for bot in bots:
            if not once and cfg.cancel_on_exit:
                bot.cancel_pending_on_exit()
            bot.state.save(bot.cfg)
        LOGGER.info("Stopped")


def load_environment() -> None:
    """Require .env beside the script and override OS environment variables."""
    if not ENV_FILE.is_file():
        raise ValueError(
            f".env not found: {ENV_FILE}. "
            "Copy the environment example to .env in the script directory"
        )
    if not load_dotenv(dotenv_path=ENV_FILE, override=True):
        raise ValueError(f"Could not load .env: {ENV_FILE}")


def main() -> int:
    parser = argparse.ArgumentParser(description="GMO Coin multi-symbol leverage bot (dry-run by default)")
    parser.add_argument("--once", action="store_true", help="Process every symbol once for verification")
    parser.add_argument("--check-private", action="store_true", help="Fetch available balance and verify private API authentication without ordering")
    parser.add_argument("--show-state", action="store_true", help="Print state files and exit")
    args = parser.parse_args()
    try:
        load_environment()
        cfg = Config.from_env()
        cfg.validate()
        setup_logging(cfg)
        if args.check_private:
            if not os.getenv("GMO_API_KEY") or not os.getenv("GMO_API_SECRET"):
                raise ValueError("--check-private requires GMO_API_KEY and GMO_API_SECRET")
            margin = GMOClient(cfg).margin()
            print(
                "Private API authentication succeeded: "
                f"actualProfitLoss={margin.get('actualProfitLoss')}, "
                f"availableAmount={margin.get('availableAmount')}. No orders were submitted."
            )
            return 0
        if args.show_state:
            states = {
                symbol: asdict(BotState.load(replace(cfg, symbol=symbol)))
                for symbol in cfg.symbols
            }
            print(json.dumps(states, ensure_ascii=False, indent=2, default=decimal_text))
            return 0
        # Prevent overlapping symbol lists in another process for the same mode.
        lock_path = cfg.data_dir / f"gmo_bot_{cfg.mode}.lock"
        with SingleInstance(lock_path):
            run_loop(cfg, args.once)
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"Configuration/startup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
