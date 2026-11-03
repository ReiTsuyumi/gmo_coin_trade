import hashlib
import hmac
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from gmo_trading_bot import (
    GMOAPIError, GMOClient, TradingBot, Book, BotState, Indicators,
    calculate_order_size, evaluate_ha_sma, evaluate_strategy,
    extract_closed_candles, extract_closed_closes, fee_gate, heikin_ashi,
    improved_limit_price, load_environment, next_cron_time, parse_book, rsi_wilder, run_loop,
    sma_series, JST,
)


class FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"status": 0, "data": 123456}


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse()


def make_candles(closes, *, last_open=None, last_low=None):
    candles = []
    previous_close = closes[0]
    for index, close in enumerate(closes):
        open_price = previous_close
        if index == len(closes) - 1 and last_open is not None:
            open_price = last_open
        low = min(open_price, close)
        if index == len(closes) - 1 and last_low is not None:
            low = last_low
        candles.append({
            "open": Decimal(open_price),
            "high": Decimal(max(open_price, close)),
            "low": Decimal(low),
            "close": Decimal(close),
        })
        previous_close = close
    return candles


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        env = {
            "DRY_RUN": "true",
            "DATA_DIR": str(Path(self.temp.name) / "data"),
            "LOG_DIR": str(Path(self.temp.name) / "logs"),
            "COOLDOWN_SECONDS": "0",
            "TRADE_CRON": "0 * * * *",
            "GMO_API_KEY": "test-key",
            "GMO_API_SECRET": "test-secret",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        from gmo_trading_bot import Config
        self.cfg = Config.from_env()
        self.cfg.validate()
        self.indicators = Indicators(
            Decimal("101"), Decimal("100"), Decimal("50"),
            True, False, True, False, "test",
        )

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def test_dotenv_is_required_and_overrides_os_environment(self):
        env_file = Path(self.temp.name) / ".env"
        with patch("gmo_trading_bot.ENV_FILE", env_file):
            with self.assertRaisesRegex(ValueError, ".env not found"):
                load_environment()
        env_file.write_text("DRY_RUN=true\nSYMBOL=ETH_JPY\n", encoding="utf-8")
        with patch.dict(os.environ, {"SYMBOL": "BTC_JPY"}):
            with patch("gmo_trading_bot.ENV_FILE", env_file):
                load_environment()
            self.assertEqual(os.getenv("SYMBOL"), "ETH_JPY")

    def test_hourly_cron_and_multiple_symbols(self):
        self.assertEqual(self.cfg.trade_cron, "0 * * * *")
        self.assertEqual(self.cfg.kline_interval, "15min")
        with patch.dict(os.environ, {"SYMBOLS": "BTC_JPY,ETH_JPY,SOL_JPY", "SYMBOL": "BTC_JPY"}):
            from gmo_trading_bot import Config
            cfg = Config.from_env()
        cfg.validate()
        self.assertEqual(cfg.symbols, ("BTC_JPY", "ETH_JPY", "SOL_JPY"))
        paths = [replace(cfg, symbol=symbol).state_path for symbol in cfg.symbols]
        self.assertEqual(len(set(paths)), 3)
        with patch.dict(os.environ, {"SYMBOLS": "BTC_JPY,BTC_JPY"}):
            from gmo_trading_bot import Config
            duplicate = Config.from_env()
        with self.assertRaisesRegex(ValueError, "unique"):
            duplicate.validate()

    def test_invalid_cron_expression_is_rejected(self):
        for expression in ("not a cron", "0 * * * * *", "61 * * * *"):
            with self.subTest(expression=expression):
                with patch.dict(os.environ, {"TRADE_CRON": expression}):
                    from gmo_trading_bot import Config
                    cfg = Config.from_env()
                with self.assertRaisesRegex(ValueError, "TRADE_CRON"):
                    cfg.validate()

    def test_next_cron_time_is_jst_and_strictly_after_current_time(self):
        current = datetime(2026, 11, 3, 12, 30, tzinfo=JST)
        self.assertEqual(
            next_cron_time("0 * * * *", current),
            datetime(2026, 11, 3, 13, 0, tzinfo=JST),
        )
        scheduled_minute = datetime(2026, 11, 3, 12, 0, tzinfo=JST)
        self.assertEqual(
            next_cron_time("*/15 * * * *", scheduled_minute),
            datetime(2026, 11, 3, 12, 15, tzinfo=JST),
        )

    def test_signature_is_hmac_of_exact_compact_body(self):
        client = GMOClient(self.cfg)
        fake = FakeSession()
        client.session = fake
        payload = {"symbol": "BTC", "side": "BUY", "price": "100"}
        client._request("POST", "private", "/v1/order", payload=payload)
        method, _, kwargs = fake.calls[0]
        body = kwargs["data"]
        headers = kwargs["headers"]
        expected_text = headers["API-TIMESTAMP"] + method + "/v1/order" + body
        expected = hmac.new(b"test-secret", expected_text.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers["API-SIGN"], expected)
        self.assertEqual(body, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))

    def test_get_signature_excludes_query_and_body(self):
        client = GMOClient(self.cfg)
        fake = FakeSession()
        client.session = fake
        client._request("GET", "private", "/v1/orders", params={"orderId": "123"})
        method, _, kwargs = fake.calls[0]
        headers = kwargs["headers"]
        expected_text = headers["API-TIMESTAMP"] + method + "/v1/orders"
        expected = hmac.new(b"test-secret", expected_text.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers["API-SIGN"], expected)
        self.assertIsNone(kwargs["data"])

    def test_orderbook_mid_and_spread(self):
        book = parse_book({
            "bids": [{"price": "999"}, {"price": "1000"}],
            "asks": [{"price": "1002"}, {"price": "1001"}],
        })
        self.assertEqual(book.best_bid, Decimal("1000"))
        self.assertEqual(book.best_ask, Decimal("1001"))
        self.assertEqual(book.mid, Decimal("1000.5"))
        self.assertEqual(book.spread, Decimal("1"))

    def test_limit_prices_improve_without_crossing(self):
        book = Book(Decimal("1000"), Decimal("1005"))
        self.assertEqual(improved_limit_price("BUY", book, self.cfg), Decimal("1001"))
        self.assertEqual(improved_limit_price("SELL", book, self.cfg), Decimal("1004"))
        one_tick = Book(Decimal("1000"), Decimal("1001"))
        self.assertEqual(improved_limit_price("BUY", one_tick, self.cfg), Decimal("1000"))
        self.assertEqual(improved_limit_price("SELL", one_tick, self.cfg), Decimal("1001"))

    def test_rsi_wilder(self):
        rising = [Decimal(i) for i in range(1, 40)]
        self.assertEqual(rsi_wilder(rising, 14), Decimal("100"))
        flat = [Decimal("100")] * 40
        self.assertEqual(rsi_wilder(flat, 14), Decimal("50"))

    def test_sma_uses_only_each_completed_window(self):
        values = [Decimal(value) for value in [10, 11, 12, 13]]
        self.assertEqual(
            sma_series(values, 2),
            [Decimal("10.5"), Decimal("11.5"), Decimal("12.5")],
        )

    def test_heikin_ashi_recurrence_and_price_bounds(self):
        candles = [
            {"open": Decimal("10"), "high": Decimal("14"), "low": Decimal("8"), "close": Decimal("12")},
            {"open": Decimal("12"), "high": Decimal("16"), "low": Decimal("10"), "close": Decimal("14")},
        ]
        result = heikin_ashi(candles)
        self.assertEqual(result[0]["open"], Decimal("11"))
        self.assertEqual(result[0]["close"], Decimal("11"))
        self.assertEqual(result[1]["open"], Decimal("11"))
        self.assertEqual(result[1]["close"], Decimal("13"))
        self.assertEqual(result[1]["high"], Decimal("16"))
        self.assertEqual(result[1]["low"], Decimal("10"))

    def test_ha_sma_buy_requires_golden_cross_and_bullish_ha(self):
        cfg = replace(
            self.cfg, strategy="HA_SMA", sma_short=2, sma_long=3,
            ha_bearish_exit_count=2,
        )
        candles = make_candles([10, 10, 10, 10, 12])
        indicators = evaluate_ha_sma(candles, cfg)
        self.assertTrue(indicators.golden_cross)
        self.assertTrue(indicators.buy_signal)
        self.assertEqual(indicators.sma_short, Decimal("11"))
        self.assertEqual(indicators.sma_long, Decimal("32") / 3)
        self.assertIn("SMA_short=11.00", indicators.reason)
        self.assertTrue(indicators.reason.isascii())

        bearish_ha = make_candles([10, 10, 10, 10, 12], last_low=1)
        blocked = evaluate_ha_sma(bearish_ha, cfg)
        self.assertTrue(blocked.golden_cross)
        self.assertFalse(blocked.buy_signal)
        self.assertFalse(blocked.sell_signal)

    def test_ha_sma_dead_cross_and_consecutive_bearish_exit(self):
        cfg = replace(
            self.cfg, strategy="HA_SMA", sma_short=2, sma_long=3,
            ha_bearish_exit_count=2,
        )
        dead = evaluate_ha_sma(make_candles([12, 12, 12, 12, 10]), cfg)
        self.assertTrue(dead.dead_cross)
        self.assertTrue(dead.sell_signal)

        early = evaluate_ha_sma(make_candles([10, 30, 30, 30, 20, 10]), cfg)
        self.assertFalse(early.dead_cross)
        self.assertEqual(early.ha_bearish_count, 2)
        self.assertTrue(early.sell_signal)

    def test_strategy_dispatch_and_ha_sma_validation(self):
        candles = make_candles(list(range(1, 30)))
        self.assertEqual(evaluate_strategy(candles, self.cfg).strategy, "EMA_RSI")
        ha_cfg = replace(self.cfg, strategy="HA_SMA", sma_short=2, sma_long=3)
        self.assertEqual(evaluate_strategy(candles, ha_cfg).strategy, "HA_SMA")
        with self.assertRaisesRegex(ValueError, "STRATEGY"):
            replace(self.cfg, strategy="UNKNOWN").validate()
        with self.assertRaisesRegex(ValueError, "SMA_SHORT"):
            replace(self.cfg, strategy="HA_SMA", sma_short=20, sma_long=5).validate()

    def test_fee_gate_uses_conservative_round_trip_cost(self):
        passed, reason = fee_gate(self.indicators, Decimal("100"), self.cfg)
        self.assertTrue(passed)
        self.assertIn("maker_round_trip_cost", reason)
        self.assertIn("conservative_taker_round_trip_cost", reason)
        self.assertTrue(reason.isascii())

    def test_position_size_targets_500_yen_and_respects_step(self):
        cfg = replace(self.cfg, min_order_size=Decimal("0.01"), size_step=Decimal("0.01"))
        size, budget, _ = calculate_order_size(
            Decimal("1000000"), Decimal("50000"), Decimal("10000"), cfg
        )
        self.assertEqual(budget, Decimal("10000"))
        self.assertEqual(size, Decimal("0.05"))
        self.assertEqual(size * Decimal("10000"), Decimal("500"))
        self.assertEqual(size % cfg.size_step, Decimal("0"))

    def test_exchange_minimum_of_2000_yen_or_more_blocks_symbol(self):
        cfg = replace(self.cfg, min_order_size=Decimal("0.001"), size_step=Decimal("0.001"))
        size, _, reason = calculate_order_size(
            Decimal("1000000"), Decimal("50000"), Decimal("2000000"), cfg
        )
        self.assertEqual(size, Decimal("0"))
        self.assertIn("skipping this symbol", reason)
        self.assertTrue(reason.isascii())

    def test_minimum_below_2000_yen_is_allowed_even_above_target(self):
        cfg = replace(self.cfg, min_order_size=Decimal("0.01"), size_step=Decimal("0.01"))
        size, _, reason = calculate_order_size(
            Decimal("1000000"), Decimal("50000"), Decimal("100000"), cfg
        )
        self.assertEqual(size, Decimal("0.01"))
        self.assertIn("notional=1000.00 JPY", reason)
        self.assertTrue(reason.isascii())

    def test_order_must_leave_3000_yen_available(self):
        size, _, reason = calculate_order_size(
            Decimal("1000000"), Decimal("3200"), Decimal("10000"),
            replace(self.cfg, min_order_size=Decimal("0.01"), size_step=Decimal("0.01")),
        )
        self.assertEqual(size, Decimal("0"))
        self.assertIn("remaining", reason)

    def test_closed_candles_are_deduplicated_and_sorted(self):
        now = datetime.now(timezone.utc)
        t1 = int((now - timedelta(minutes=3)).timestamp() * 1000)
        t2 = int((now - timedelta(minutes=2)).timestamp() * 1000)
        current = [{"openTime": str(t2), "close": "102"}]
        previous = [{"openTime": str(t1), "close": "101"}, {"openTime": str(t2), "close": "999"}]
        self.assertEqual(
            extract_closed_closes(current, previous, now),
            [Decimal("101"), Decimal("102")],
        )

    def test_15min_candles_exclude_unclosed_candle(self):
        now = datetime.now(timezone.utc)
        current_open = now - timedelta(minutes=5)
        closed_open = now - timedelta(minutes=20)
        rows = [
            {
                "openTime": str(int(closed_open.timestamp() * 1000)),
                "open": "100", "high": "103", "low": "99", "close": "102",
            },
            {
                "openTime": str(int(current_open.timestamp() * 1000)),
                "open": "102", "high": "110", "low": "101", "close": "109",
            },
        ]
        result = extract_closed_candles(rows, [], now, "15min")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["close"], Decimal("102"))

    def test_closed_ohlc_candles_exclude_current_and_keep_deduplicated_values(self):
        now = datetime.now(timezone.utc)
        t1 = int((now - timedelta(minutes=3)).timestamp() * 1000)
        t2 = int((now - timedelta(minutes=2)).timestamp() * 1000)
        current = [{
            "openTime": str(t2), "open": "101", "high": "104",
            "low": "100", "close": "102",
        }]
        previous = [
            {"openTime": str(t1), "open": "100", "high": "102", "low": "99", "close": "101"},
            {"openTime": str(t2), "open": "1", "high": "2", "low": "0", "close": "999"},
        ]
        result = extract_closed_candles(current, previous, now)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[-1]["open"], Decimal("101"))
        self.assertEqual(result[-1]["high"], Decimal("104"))
        self.assertEqual(result[-1]["low"], Decimal("100"))
        self.assertEqual(result[-1]["close"], Decimal("102"))

    def test_daily_loss_limit_latches(self):
        bot = TradingBot(self.cfg)
        bot.state.day_start_equity = Decimal("1000000")
        self.assertFalse(bot.daily_loss_exceeded(Decimal("950001")))
        self.assertTrue(bot.daily_loss_exceeded(Decimal("950000")))
        self.assertTrue(bot.daily_loss_exceeded(Decimal("990000")))

    def test_dry_run_fill_and_fee_adjusted_realized_pnl(self):
        bot = TradingBot(self.cfg)
        book = Book(Decimal("999"), Decimal("1000"))
        bot.state.pending_order = {
            "order_id": "dry-buy", "side": "BUY", "price": "1000", "size": "0.1",
            "reason": "test", "risk_budget": "1000", "applied_size": "0",
        }
        bot.reconcile_dry_order(book, self.indicators)
        self.assertEqual(bot.state.position_size, Decimal("0.1"))
        expected_cost = Decimal("100") + Decimal("0.05")
        self.assertEqual(bot.state.position_cost, expected_cost)
        self.assertEqual(bot.state.cash, Decimal("1000000") - Decimal("0.05"))
        self.assertEqual(bot.state.position_side, "BUY")

        sell_book = Book(Decimal("1100"), Decimal("1101"))
        bot.state.pending_order = {
            "order_id": "dry-sell", "side": "SELL", "settle_type": "CLOSE",
            "position_id": bot.state.position_id, "price": "1100", "size": "0.1",
            "reason": "test", "risk_budget": "0", "applied_size": "0",
        }
        bot.reconcile_dry_order(sell_book, self.indicators)
        self.assertEqual(bot.state.position_size, Decimal("0"))
        # Sale proceeds 110 - closing fee 0.055 - opening cost 100.05 = 9.895 JPY.
        self.assertEqual(bot.state.total_realized_pnl, Decimal("9.895"))
        self.assertIsNone(bot.state.pending_order)
        csv_files = list(self.cfg.log_dir.glob("decisions_*.csv"))
        self.assertEqual(len(csv_files), 1)
        csv_text = csv_files[0].read_text(encoding="utf-8-sig")
        self.assertTrue(csv_text.isascii())
        self.assertIn("dry-run filled in full", csv_text)

    def test_dry_run_short_position_profits_when_price_falls(self):
        bot = TradingBot(self.cfg)
        book = Book(Decimal("1000"), Decimal("1001"))
        bot.state.pending_order = {
            "order_id": "dry-short", "side": "SELL", "settle_type": "OPEN",
            "price": "1000", "size": "0.1", "reason": "test",
            "risk_budget": "1000", "applied_size": "0",
        }
        bot.reconcile_dry_order(book, self.indicators)
        self.assertEqual(bot.state.position_side, "SELL")
        self.assertEqual(bot.state.position_entry_price, Decimal("1000"))
        close_book = Book(Decimal("899"), Decimal("900"))
        bot.state.pending_order = {
            "order_id": "dry-cover", "side": "BUY", "settle_type": "CLOSE",
            "position_id": "dry-position", "price": "900", "size": "0.1",
            "reason": "test", "risk_budget": "0", "applied_size": "0",
        }
        bot.reconcile_dry_order(close_book, self.indicators)
        self.assertEqual(bot.state.position_size, Decimal("0"))
        self.assertEqual(bot.state.total_realized_pnl, Decimal("9.905"))
        self.assertEqual(bot.state.cash, Decimal("1000009.905"))

    def test_forced_loss_cutoff_at_50_bypasses_spread_and_cooldown(self):
        bot = TradingBot(self.cfg)
        bot.state.position_size = Decimal("0.1")
        bot.state.position_side = "BUY"
        bot.state.position_id = "dry-position"
        bot.state.position_entry_price = Decimal("1000")
        bot.state.last_order_at = datetime.now(timezone.utc).isoformat()
        bot.client.status = Mock(return_value="OPEN")
        bot.client.symbol_rule = Mock(return_value={
            "tickSize": "1", "sizeStep": "0.0001", "minOrderSize": "0.0001",
            "makerFee": "0", "takerFee": "0",
        })
        bot.client.orderbook = Mock(return_value={
            "bids": [{"price": "500"}], "asks": [{"price": "501"}],
        })
        bot.candles = Mock(return_value=make_candles(list(range(1, 40))))
        bot.run_once()
        pending = bot.state.pending_order
        self.assertIsNotNone(pending)
        self.assertEqual(pending["settle_type"], "CLOSE")
        self.assertEqual(pending["side"], "SELL")
        self.assertEqual(pending["price"], "500")
        self.assertIn("FORCED_LOSS_CUTOFF", pending["reason"])
        self.assertTrue(pending["reason"].isascii())

    def test_profitable_long_is_held_in_uptrend(self):
        bot = TradingBot(self.cfg)
        bot.state.position_size = Decimal("0.1")
        bot.state.position_side = "BUY"
        bot.state.position_entry_price = Decimal("1000")
        book = Book(Decimal("1100"), Decimal("1101"))
        self.assertGreater(bot.estimated_close_pnl(book), Decimal("0"))
        self.assertTrue(bot.favorable_trend(self.indicators))

    def test_profitable_short_is_held_in_downtrend(self):
        bot = TradingBot(self.cfg)
        bot.state.position_size = Decimal("0.1")
        bot.state.position_side = "SELL"
        bot.state.position_entry_price = Decimal("1000")
        falling = Indicators(
            Decimal("99"), Decimal("100"), Decimal("50"),
            False, True, False, True, "test",
        )
        book = Book(Decimal("899"), Decimal("900"))
        self.assertGreater(bot.estimated_close_pnl(book), Decimal("0"))
        self.assertTrue(bot.favorable_trend(falling))

    def test_forced_loss_cutoff_applies_to_short_positions(self):
        bot = TradingBot(self.cfg)
        bot.state.position_size = Decimal("0.1")
        bot.state.position_side = "SELL"
        bot.state.position_entry_price = Decimal("1000")
        pnl = bot.estimated_close_pnl(Book(Decimal("1499"), Decimal("1500")))
        self.assertEqual(pnl, Decimal("-50.07500"))

    def test_close_order_uses_position_id(self):
        client = GMOClient(self.cfg)
        fake = FakeSession()
        client.session = fake
        client.place_close_limit_order(
            "BUY", Decimal("900"), Decimal("0.1"), "123", "client-id"
        )
        body = json.loads(fake.calls[0][2]["data"])
        self.assertEqual(body["settlePosition"], {"positionId": 123, "size": "0.1"})
        self.assertEqual(body["side"], "BUY")

    def test_existing_position_never_allows_another_open(self):
        bot = TradingBot(self.cfg)
        bot.state.position_size = Decimal("0.1")
        bot.state.position_cost = Decimal("100000")
        with self.assertRaisesRegex(RuntimeError, "Additional positions are forbidden"):
            bot.submit_order(
                side="BUY", price=Decimal("1000"), size=Decimal("0.1"), reason="test",
                risk_budget=Decimal("1000"), book=Book(Decimal("999"), Decimal("1001")),
                indicators=self.indicators, equity=Decimal("1000000"),
                available_jpy=Decimal("1000000"), available_base=Decimal("0"),
            )
        self.assertIsNone(bot.state.pending_order)

    def test_definite_order_rejection_clears_pending(self):
        bot = TradingBot(replace(self.cfg, dry_run=False))
        def reject(*_):
            raise GMOAPIError("rejected")
        bot.client.place_limit_order = reject
        with self.assertRaises(GMOAPIError):
            bot.submit_order(
                side="BUY", price=Decimal("1000"), size=Decimal("0.1"), reason="test",
                risk_budget=Decimal("1000"), book=Book(Decimal("999"), Decimal("1001")),
                indicators=self.indicators, equity=Decimal("1000000"),
                available_jpy=Decimal("1000000"), available_base=Decimal("0"),
            )
        self.assertIsNone(bot.state.pending_order)

    def test_ambiguous_order_result_keeps_pending_for_reconciliation(self):
        bot = TradingBot(replace(self.cfg, dry_run=False))
        def timeout(*_):
            raise RuntimeError("timeout")
        bot.client.place_limit_order = timeout
        with self.assertRaises(RuntimeError):
            bot.submit_order(
                side="BUY", price=Decimal("1000"), size=Decimal("0.1"), reason="test",
                risk_budget=Decimal("1000"), book=Book(Decimal("999"), Decimal("1001")),
                indicators=self.indicators, equity=Decimal("1000000"),
                available_jpy=Decimal("1000000"), available_base=Decimal("0"),
            )
        self.assertIsNotNone(bot.state.pending_order)

    def test_once_processes_each_symbol_independently(self):
        cfg = replace(self.cfg, symbols=("BTC_JPY", "ETH_JPY", "SOL_JPY"))
        bots = []
        for symbol in cfg.symbols:
            bot = Mock()
            bot.cfg = replace(cfg, symbol=symbol)
            bots.append(bot)
        with patch("gmo_trading_bot.TradingBot", side_effect=bots):
            run_loop(cfg, once=True)
        for bot in bots:
            bot.run_once.assert_called_once_with()
            bot.cancel_pending_on_exit.assert_not_called()
            bot.state.save.assert_called_once_with(bot.cfg)

    def test_continuous_mode_waits_for_cron_and_stops_without_catch_up(self):
        bot = Mock()
        bot.cfg = self.cfg
        stop_event = Mock()
        stop_event.is_set.return_value = False
        stop_event.wait.side_effect = [False, True]
        current = datetime(2026, 11, 3, 12, 30, tzinfo=JST)
        next_run = datetime(2026, 11, 3, 13, 0, tzinfo=JST)
        following_run = datetime(2026, 11, 3, 14, 0, tzinfo=JST)
        with (
            patch("gmo_trading_bot.TradingBot", return_value=bot),
            patch("gmo_trading_bot.threading.Event", return_value=stop_event),
            patch("gmo_trading_bot.utc_now", return_value=current),
            patch("gmo_trading_bot.next_cron_time", side_effect=[next_run, following_run]) as schedule,
        ):
            run_loop(self.cfg, once=False)
        bot.run_once.assert_called_once_with()
        stop_event.wait.assert_any_call(1800.0)
        self.assertEqual(schedule.call_count, 2)

    def test_state_round_trip(self):
        state = BotState(symbol=self.cfg.symbol, mode="dry-run", cash=Decimal("123.45"))
        state.position_size = Decimal("0.01")
        state.save(self.cfg)
        loaded = BotState.load(self.cfg)
        self.assertEqual(loaded.cash, Decimal("123.45"))
        self.assertEqual(loaded.position_size, Decimal("0.01"))


if __name__ == "__main__":
    unittest.main()
