import asyncio
import json
import re
import unittest
from decimal import Decimal
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

from aioresponses import aioresponses
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_constants as CONSTANTS
import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_web_utils as web_utils
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_derivative import ArcusPerpetualDerivative
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import OrderState
from hummingbot.core.data_type.order_candidate import PerpetualOrderCandidate
from hummingbot.core.event.event_listener import EventListener
from hummingbot.core.event.events import MarketEvent, OrderCancelledEvent, OrderFilledEvent


class QueueEventListener(EventListener):
    def __init__(self) -> None:
        super().__init__()
        self.events: asyncio.Queue[OrderCancelledEvent | OrderFilledEvent] = asyncio.Queue()

    def __call__(self, event: OrderCancelledEvent | OrderFilledEvent) -> None:
        self.events.put_nowait(event)


class ArcusPerpetualDerivativeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def create_connector() -> ArcusPerpetualDerivative:
        return ArcusPerpetualDerivative(
            trading_pairs=["BTC-USD"],
            trading_required=False,
        )

    @staticmethod
    def create_authenticated_connector() -> ArcusPerpetualDerivative:
        return ArcusPerpetualDerivative(
            api_signing_key=bytes(range(32)).hex(),
            account_address="0x" + "ab" * 20,
            account_index=2,
            trading_pairs=["BTC-USD"],
            trading_required=False,
        )

    @staticmethod
    def btc_market() -> Dict[str, Any]:
        return {
            "marketDisplayName": "BTC-USD",
            "marketId": 1,
            "status": "ONLINE",
            "type": "PERPETUAL",
            "baseAsset": "BTC",
            "quoteAsset": "USD",
            "tickSize": "0.1",
            "stepSize": "0.00000001",
            "minOrderSize": "0.0001",
            "maxOrderSize": "10000",
            "minOrderNotional": "5",
            "tickTiers": [
                {"upToPrice": "500000", "tick": "0.1"},
                {"tick": "0.2"},
            ],
            "markPrice": "50000",
            "oraclePrice": "50000",
            "lastTradePrice": "50000",
            "fundingRate": "0.0001",
            "nextFundingAt": 1_700_003_600,
            "initialMarginFraction": "0.1",
        }

    @aioresponses()
    async def test_market_metadata_is_cached_and_filters_non_perpetual_markets(self, mock_api):
        connector = self.create_connector()
        url = web_utils.public_rest_url(CONSTANTS.MARKETS_PATH)
        mock_api.get(
            url,
            body=json.dumps(
                {
                    "markets": [
                        self.btc_market(),
                        {"marketDisplayName": "BTC-SPOT", "type": "SPOT"},
                    ]
                }
            ),
        )

        first_result = await connector._get_markets()
        second_result = await connector._get_markets()

        self.assertEqual({"BTC-USD"}, set(first_result))
        self.assertIs(first_result, second_result)
        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": list(first_result.values())}
        )
        self.assertIs(first_result["BTC-USD"], await connector._get_market_info("BTC-USD"))
        self.assertEqual(50_000.0, await connector._get_last_traded_price("BTC-USD"))

    @aioresponses()
    async def test_update_trading_rules_fetches_markets_maps_symbols_and_leverage(self, mock_api):
        connector = self.create_connector()
        url = web_utils.public_rest_url(CONSTANTS.MARKETS_PATH)
        mock_api.get(url, body=json.dumps({"markets": [self.btc_market()]}))

        await connector._update_trading_rules()

        self.assertEqual(Decimal("0.1"), connector._trading_rules["BTC-USD"].min_price_increment)
        self.assertEqual("BTC-USD", await connector.exchange_symbol_associated_to_pair("BTC-USD"))
        self.assertEqual(10, connector.get_leverage("BTC-USD"))

    async def test_connector_reports_supported_orders_modes_and_collateral(self):
        connector = self.create_connector()
        rules = await connector._format_trading_rules({"markets": [self.btc_market()]})
        connector._trading_rules["BTC-USD"] = rules[0]

        self.assertEqual(CONSTANTS.DEFAULT_DOMAIN, connector.name)
        self.assertEqual(CONSTANTS.DEFAULT_DOMAIN, connector.domain)
        self.assertIs(connector.authenticator, connector._arcus_auth)
        self.assertIs(CONSTANTS.RATE_LIMITS, connector.rate_limits_rules)
        self.assertEqual(32, connector.client_order_id_max_length)
        self.assertEqual("hb", connector.client_order_id_prefix)
        self.assertEqual(CONSTANTS.MARKETS_PATH, connector.trading_rules_request_path)
        self.assertEqual(CONSTANTS.MARKETS_PATH, connector.trading_pairs_request_path)
        self.assertEqual(CONSTANTS.HEALTH_PATH, connector.check_network_request_path)
        self.assertEqual(["BTC-USD"], connector.trading_pairs)
        self.assertFalse(connector.is_cancel_request_in_exchange_synchronous)
        self.assertFalse(connector.is_trading_required)
        self.assertEqual(60, connector.funding_fee_poll_interval)
        self.assertEqual(
            [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET],
            connector.supported_order_types(),
        )
        self.assertEqual([PositionMode.ONEWAY], connector.supported_position_modes())
        self.assertEqual("USDG", connector.get_buy_collateral_token("BTC-USD"))
        self.assertEqual("USDG", connector.get_sell_collateral_token("BTC-USD"))

        one_way_set, one_way_message = await connector._trading_pair_position_mode_set(
            PositionMode.ONEWAY, "BTC-USD"
        )
        hedge_set, hedge_message = await connector._trading_pair_position_mode_set(
            PositionMode.HEDGE, "BTC-USD"
        )
        self.assertTrue(one_way_set)
        self.assertEqual("", one_way_message)
        self.assertFalse(hedge_set)
        self.assertIn("one-way", hedge_message)
        self.assertIsNone(await connector._update_trading_fees())

    async def test_budget_checker_uses_usdg_collateral_without_synthetic_conversion_books(self):
        connector = self.create_connector()
        rules = await connector._format_trading_rules({"markets": [self.btc_market()]})
        connector._trading_rules["BTC-USD"] = rules[0]
        connector._account_balances["USDG"] = Decimal("1000")
        connector._account_available_balances["USDG"] = Decimal("1000")

        candidates = [
            PerpetualOrderCandidate(
                trading_pair="BTC-USD",
                is_maker=True,
                order_type=OrderType.LIMIT_MAKER,
                order_side=side,
                amount=Decimal("0.001"),
                price=Decimal("50000"),
                leverage=Decimal("10"),
                position_close=False,
            )
            for side in (TradeType.BUY, TradeType.SELL)
        ]

        adjusted = connector.budget_checker.adjust_candidates(candidates)

        for candidate in adjusted:
            self.assertEqual("USDG", candidate.order_collateral.token)
            self.assertEqual(Decimal("5"), candidate.order_collateral.amount)
            self.assertEqual("USDG", candidate.percent_fee_collateral.token)
            self.assertEqual(Decimal("0.025"), candidate.percent_fee_collateral.amount)
            self.assertEqual(Decimal("0.001"), candidate.amount)

    async def test_order_not_found_classifiers_match_http_404_only(self):
        connector = self.create_connector()

        self.assertFalse(connector._is_request_exception_related_to_time_synchronizer(RuntimeError()))
        self.assertTrue(connector._is_order_not_found_during_status_update_error(Exception("HTTP status is 404")))
        self.assertFalse(connector._is_order_not_found_during_status_update_error(Exception("HTTP status is 500")))
        self.assertTrue(connector._is_order_not_found_during_cancelation_error(Exception("HTTP status is 404")))
        self.assertFalse(connector._is_order_not_found_during_cancelation_error(Exception("HTTP status is 500")))

    async def test_market_rules_map_size_price_and_usdg_collateral(self):
        connector = self.create_connector()
        rules = await connector._format_trading_rules(
            {
                "markets": [
                    {
                        "marketDisplayName": "BTC-USD",
                        "marketId": 1,
                        "type": "PERPETUAL",
                        "status": "ONLINE",
                        "baseAsset": "BTC",
                        "quoteAsset": "USD",
                        "tickSize": "0.1",
                        "stepSize": "0.00000001",
                        "minOrderSize": "0.0001",
                        "maxOrderSize": "10000",
                        "minOrderNotional": "5",
                    },
                    {
                        "marketDisplayName": "AAPL",
                        "marketId": 100,
                        "type": "SPOT",
                        "status": "ONLINE",
                        "baseAsset": "AAPL",
                        "quoteAsset": "USD",
                        "tickSize": "0.01",
                        "stepSize": "1",
                        "minOrderSize": "1",
                        "maxOrderSize": "100",
                        "minOrderNotional": "1",
                    },
                    {
                        "marketDisplayName": "OFF-USD",
                        "marketId": 2,
                        "type": "PERPETUAL",
                        "status": "OFFLINE",
                        "baseAsset": "OFF",
                        "quoteAsset": "USD",
                        "tickSize": "0.1",
                        "stepSize": "0.01",
                        "minOrderSize": "1",
                        "maxOrderSize": "100",
                        "minOrderNotional": "5",
                    },
                ]
            }
        )

        self.assertEqual(1, len(rules))
        rule = rules[0]
        self.assertEqual("BTC-USD", rule.trading_pair)
        self.assertEqual(Decimal("0.0001"), rule.min_order_size)
        self.assertEqual(Decimal("10000"), rule.max_order_size)
        self.assertEqual(Decimal("0.1"), rule.min_price_increment)
        self.assertEqual(Decimal("0.00000001"), rule.min_base_amount_increment)
        self.assertEqual(Decimal("5"), rule.min_notional_size)
        self.assertEqual("USDG", rule.buy_order_collateral_token)
        self.assertEqual("USDG", rule.sell_order_collateral_token)

    async def test_tier_tick_rounds_buy_down_and_sell_up_in_base_tick_units(self):
        connector = self.create_connector()
        market = {
            "tickSize": "0.1",
            "tickTiers": [
                {"upToPrice": "500000", "tick": "0.1"},
                {"upToPrice": "1000000", "tick": "0.2"},
                {"tick": "0.5"},
            ],
        }

        buy_price, buy_ticks = connector._quantize_price_to_tick(
            market, Decimal("500000.21"), TradeType.BUY
        )
        sell_price, sell_ticks = connector._quantize_price_to_tick(
            market, Decimal("500000.01"), TradeType.SELL
        )

        self.assertEqual(Decimal("500000.2"), buy_price)
        self.assertEqual(5_000_002, buy_ticks)
        self.assertEqual(Decimal("500000.2"), sell_price)
        self.assertEqual(5_000_002, sell_ticks)

    async def test_quantity_must_be_exact_step_multiple(self):
        connector = self.create_connector()
        market = {"stepSize": "0.0001"}

        self.assertEqual(12, connector._quantity_quantums(market, Decimal("0.0012")))
        with self.assertRaisesRegex(ValueError, "not a multiple"):
            connector._quantity_quantums(market, Decimal("0.00125"))

    @aioresponses()
    async def test_place_order_signs_tick_and_quantum_payload_with_nanosecond_clock(self, mock_api):
        connector = self.create_authenticated_connector()
        connector._get_market_info = AsyncMock(return_value=self.btc_market())
        connector._time_synchronizer.time = MagicMock(return_value=1_700_000_000.0)
        mock_api.post(
            web_utils.private_rest_url(CONSTANTS.PLACE_ORDER_PATH),
            body=json.dumps({"orderId": "arcus-order-1"}),
        )

        exchange_order_id, _ = await connector._place_order(
            order_id="hb-test-order",
            trading_pair="BTC-USD",
            amount=Decimal("0.001"),
            trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal("50000.1"),
            position_action=PositionAction.OPEN,
        )

        request = next(iter(mock_api.requests.values()))[0]
        request_data = json.loads(request.kwargs["data"])
        request_headers = request.kwargs["headers"]
        timestamp_ns = 1_700_000_000_000_000_000
        self.assertEqual("arcus-order-1", exchange_order_id)
        self.assertEqual(str(timestamp_ns), request_headers["X-Timestamp"])
        self.assertEqual("0.001", request_data["quantity"])
        self.assertEqual("GTT", request_data["timeInForce"])
        self.assertEqual(
            str(timestamp_ns // 1000 + 40 * 24 * 60 * 60 * 1_000_000),
            request_data["goodTilTime"],
        )

        good_til_ns = int(request_data["goodTilTime"]) * 1000
        address = "0x" + "ab" * 20
        payload = (
            f'{{"ad":"{address}","ai":2,"c":"hb-test-order","ct":{timestamp_ns},'
            f'"g":{good_til_ns},"m":1,"op":1,"p":500001,"q":100000,'
            f'"r":0,"s":0,"t":0,"v":1}}'
        )
        public_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key()
        public_key.verify(
            bytes.fromhex(request_headers["X-Signature"]),
            payload.encode("utf-8"),
        )

    @aioresponses()
    async def test_cancel_by_exchange_id_does_not_send_a_second_cancel_target(self, mock_api):
        connector = self.create_authenticated_connector()
        connector._get_market_info = AsyncMock(return_value=self.btc_market())
        connector._time_synchronizer.time = MagicMock(return_value=1_700_000_000.0)
        mock_api.post(
            web_utils.private_rest_url(CONSTANTS.CANCEL_ORDER_PATH),
            body=json.dumps({"status": "CANCEL_ACKNOWLEDGED"}),
        )
        order = MagicMock()
        order.client_order_id = "hb-test-order"
        order.exchange_order_id = "arcus-order-1"
        order.trading_pair = "BTC-USD"

        success = await connector._place_cancel(order.client_order_id, order)

        request = next(iter(mock_api.requests.values()))[0]
        request_data = json.loads(request.kwargs["data"])
        self.assertTrue(success)
        self.assertEqual("orderId", request_data["kind"])
        self.assertEqual("arcus-order-1", request_data["orderId"])
        self.assertNotIn("clientId", request_data)
        self.assertEqual(
            str(1_700_000_000_000_000_000),
            request.kwargs["headers"]["X-Timestamp"],
        )

    async def test_place_order_rejects_below_minimum_notional_without_api_write(self):
        connector = self.create_authenticated_connector()
        connector._get_market_info = AsyncMock(return_value=self.btc_market())
        connector._api_post = AsyncMock()
        connector._time_synchronizer.time = MagicMock(return_value=1_700_000_000.0)

        with self.assertRaisesRegex(ValueError, "below minimum"):
            await connector._place_order(
                order_id="hb-small",
                trading_pair="BTC-USD",
                amount=Decimal("0.0001"),
                trade_type=TradeType.BUY,
                order_type=OrderType.LIMIT,
                price=Decimal("10000"),
                position_action=PositionAction.OPEN,
            )

        connector._api_post.assert_not_awaited()

    @aioresponses()
    async def test_account_balance_and_positions_use_mocked_rest_endpoints(self, mock_api):
        connector = self.create_authenticated_connector()
        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": [self.btc_market()]}
        )
        account_url = web_utils.public_rest_url(CONSTANTS.ACCOUNT_PATH)
        positions_url = web_utils.public_rest_url(CONSTANTS.POSITIONS_PATH)
        mock_api.get(
            re.compile(f"^{re.escape(account_url)}\\?.*$"),
            body=json.dumps({"equity": "120.5", "freeCollateral": "80"}),
        )
        mock_api.get(
            re.compile(f"^{re.escape(positions_url)}\\?.*$"),
            body=json.dumps(
                {
                    "positions": {
                        "1": {
                            "marketDisplayName": "BTC-USD",
                            "size": "0.02",
                            "averageEntryPrice": "50000",
                            "unrealizedPnl": "4.5",
                            "leverage": "5",
                        }
                    }
                }
            ),
        )

        await connector._update_balances()
        await connector._update_positions()

        self.assertEqual(Decimal("120.5"), connector.get_balance("USDG"))
        self.assertEqual(Decimal("80"), connector.get_available_balance("USDG"))
        position = connector._perpetual_trading.get_position("BTC-USD", PositionSide.LONG)
        if position is None:
            self.fail("Expected REST position snapshot to be tracked.")
        self.assertEqual(Decimal("0.02"), position.amount)
        self.assertEqual(Decimal("4.5"), position.unrealized_pnl)
        requests_by_path = {
            str(url).split("?", 1)[0].rsplit("/", 1)[-1]: calls[0]
            for (_, url), calls in mock_api.requests.items()
        }
        expected_params = {"address": "0x" + "ab" * 20, "accountIndex": 2}
        self.assertEqual(expected_params, requests_by_path["account"].kwargs["params"])
        self.assertEqual(expected_params, requests_by_path["positions"].kwargs["params"])
        self.assertEqual(
            connector.authenticator.api_key,
            requests_by_path["account"].kwargs["headers"]["X-API-Key"],
        )

    @aioresponses()
    async def test_order_status_is_parsed_from_mocked_rest_response(self, mock_api):
        connector = self.create_authenticated_connector()
        connector.start_tracking_order(
            order_id="hb-status",
            exchange_order_id="arcus-status",
            trading_pair="BTC-USD",
            trade_type=TradeType.BUY,
            price=Decimal("50000"),
            amount=Decimal("0.002"),
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
        )
        url = web_utils.public_rest_url(f"{CONSTANTS.ORDER_PATH}/arcus-status")
        mock_api.get(
            re.compile(f"^{re.escape(url)}\\?.*$"),
            body=json.dumps(
                {
                    "order": {
                        "orderId": "arcus-status",
                        "status": "OPEN",
                        "updatedAt": 1_700_000_000_000_000,
                    }
                }
            ),
        )

        update = await connector._request_order_status(
            connector._order_tracker.all_orders["hb-status"]
        )

        self.assertEqual(OrderState.OPEN, update.new_state)
        self.assertEqual(1_700_000_000, update.update_timestamp)
        self.assertEqual("hb-status", update.client_order_id)
        self.assertEqual("arcus-status", update.exchange_order_id)
        request = next(iter(mock_api.requests.values()))[0]
        self.assertEqual(
            {"address": "0x" + "ab" * 20, "accountIndex": 2},
            request.kwargs["params"],
        )
        self.assertEqual(
            connector.authenticator.api_key,
            request.kwargs["headers"]["X-API-Key"],
        )

    async def test_account_balance_and_signed_position_fields_map_to_hummingbot(self):
        connector = self.create_authenticated_connector()
        connector._api_get = AsyncMock(
            return_value={
                "equity": "120.5",
                "freeCollateral": "80",
            }
        )
        await connector._update_balances()
        self.assertEqual(Decimal("120.5"), connector.get_balance("USDG"))
        self.assertEqual(Decimal("80"), connector.get_available_balance("USDG"))

        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": [self.btc_market()]}
        )
        connector._api_get = AsyncMock(
            return_value={
                "positions": {
                    "1": {
                        "marketId": 1,
                        "marketDisplayName": "BTC-USD",
                        "side": "LONG",
                        "size": "0.02",
                        "averageEntryPrice": "50000",
                        "unrealizedPnl": "4.5",
                        "leverage": "5",
                    }
                }
            }
        )

        await connector._update_positions()
        position = connector._perpetual_trading.get_position("BTC-USD", PositionSide.LONG)
        if position is None:
            self.fail("Expected Arcus long position to be tracked.")
        self.assertEqual(Decimal("0.02"), position.amount)
        self.assertEqual(Decimal("4.5"), position.unrealized_pnl)

    async def test_user_stream_account_and_position_snapshot_refreshes_state(self):
        connector = self.create_authenticated_connector()
        btc_market = self.btc_market()
        eth_market = {
            **btc_market,
            "marketDisplayName": "ETH-USD",
            "marketId": 2,
            "baseAsset": "ETH",
        }
        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": [btc_market, eth_market]}
        )
        existing_btc = await connector._position_from_market_data(
            {
                "marketDisplayName": "BTC-USD",
                "size": "0.02",
                "averageEntryPrice": "50000",
                "unrealizedPnl": "4.5",
                "leverage": "5",
            }
        )
        if existing_btc is None:
            self.fail("Expected initial BTC position to parse.")
        connector._perpetual_trading.set_position(
            connector._perpetual_trading.position_key("BTC-USD", PositionSide.LONG),
            existing_btc,
        )

        async def user_events():
            yield {
                "channel": "account",
                "contents": {"equity": "140", "freeCollateral": "90"},
            }
            yield {
                "channel": "positions",
                "contents": {
                    "isSnapshot": True,
                    "positions": {
                        "2": {
                            "marketDisplayName": "ETH-USD",
                            "size": "1.5",
                            "averageEntryPrice": "3000",
                            "unrealizedPnl": "12",
                            "leverage": "3",
                        }
                    },
                },
            }

        connector._iter_user_event_queue = user_events

        await connector._user_stream_event_listener()

        self.assertEqual(Decimal("140"), connector.get_balance("USDG"))
        self.assertEqual(Decimal("90"), connector.get_available_balance("USDG"))
        self.assertIsNone(connector._perpetual_trading.get_position("BTC-USD", PositionSide.LONG))
        eth_position = connector._perpetual_trading.get_position("ETH-USD", PositionSide.LONG)
        if eth_position is None:
            self.fail("Expected position snapshot to replace stale BTC position with ETH.")
        self.assertEqual(Decimal("1.5"), eth_position.amount)

    async def test_flat_position_stream_row_removes_existing_position(self):
        connector = self.create_authenticated_connector()
        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": [self.btc_market()]}
        )
        opened = await connector._position_from_market_data(
            {
                "marketDisplayName": "BTC-USD",
                "side": "LONG",
                "size": "0.02",
                "averageEntryPrice": "50000",
                "unrealizedPnl": "4.5",
                "leverage": "5",
            }
        )
        if opened is None:
            self.fail("Expected Arcus open position to parse.")
        key = connector._perpetual_trading.position_key("BTC-USD", PositionSide.LONG)
        connector._perpetual_trading.set_position(key, opened)

        closed = await connector._position_from_market_data(
            {
                "marketDisplayName": "BTC-USD",
                "side": "LONG",
                "status": "FLAT",
                "size": "0",
            }
        )

        self.assertIsNone(closed)
        self.assertIsNone(connector._perpetual_trading.get_position("BTC-USD", PositionSide.LONG))

    async def test_orders_channel_event_applies_terminal_state_to_tracked_order(self):
        connector = self.create_authenticated_connector()
        connector._time_synchronizer.time = MagicMock(return_value=1_700_000_000.0)
        connector.start_tracking_order(
            order_id="hb-terminal",
            exchange_order_id="arcus-terminal",
            trading_pair="BTC-USD",
            trade_type=TradeType.BUY,
            price=Decimal("50000"),
            amount=Decimal("0.002"),
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
        )

        async def user_events():
            yield {
                "channel": "orders",
                "type": "channel_data",
                "contents": {
                    "clientId": "hb-terminal",
                    "orderId": "arcus-terminal",
                    "status": "CANCELED",
                    "updatedAt": 1_700_000_000_000_000,
                },
            }

        event_listener = QueueEventListener()
        connector.add_listener(MarketEvent.OrderCancelled, event_listener)
        connector._iter_user_event_queue = user_events
        await connector._user_stream_event_listener()
        cancelled_event: OrderCancelledEvent | OrderFilledEvent = await asyncio.wait_for(
            event_listener.events.get(), timeout=1
        )

        self.assertEqual("hb-terminal", cancelled_event.order_id)
        self.assertEqual(OrderState.CANCELED, connector._order_tracker.all_orders["hb-terminal"].current_state)

    async def test_user_fill_updates_executed_amount(self):
        connector = self.create_authenticated_connector()
        connector._time_synchronizer.time = MagicMock(return_value=1_700_000_000.0)
        connector.start_tracking_order(
            order_id="hb-fill",
            exchange_order_id="arcus-fill",
            trading_pair="BTC-USD",
            trade_type=TradeType.BUY,
            price=Decimal("50000"),
            amount=Decimal("0.002"),
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
        )

        async def user_events():
            yield {
                "channel": "userFills",
                "type": "channel_data",
                "contents": {
                    "clientId": "hb-fill",
                    "orderId": "arcus-fill",
                    "tradeId": "trade-1",
                    "marketDisplayName": "BTC-USD",
                    "price": "50000",
                    "size": "0.001",
                    "fee": "0.05",
                    "createdAt": 1_700_000_000_000_000,
                },
            }

        event_listener = QueueEventListener()
        connector.add_listener(MarketEvent.OrderFilled, event_listener)
        connector._iter_user_event_queue = user_events
        await connector._user_stream_event_listener()
        fill_event: OrderCancelledEvent | OrderFilledEvent = await asyncio.wait_for(
            event_listener.events.get(), timeout=1
        )

        order = connector._order_tracker.all_orders["hb-fill"]
        if not isinstance(fill_event, OrderFilledEvent):
            self.fail("Expected fill event from Arcus user stream.")
        self.assertEqual("trade-1", fill_event.exchange_trade_id)
        self.assertEqual(Decimal("0.001"), order.executed_amount_base)

    @aioresponses()
    async def test_fill_poll_reconciles_mocked_rest_history(self, mock_api):
        connector = self.create_authenticated_connector()
        connector._time_synchronizer.time = MagicMock(return_value=1_700_000_000.0)
        connector._set_current_timestamp(1_700_000_000.0)
        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": [self.btc_market()]}
        )
        connector.start_tracking_order(
            order_id="hb-fill-poll",
            exchange_order_id="arcus-fill-poll",
            trading_pair="BTC-USD",
            trade_type=TradeType.BUY,
            price=Decimal("50000"),
            amount=Decimal("0.002"),
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
        )
        listener = QueueEventListener()
        connector.add_listener(MarketEvent.OrderFilled, listener)
        url = web_utils.public_rest_url(CONSTANTS.FILLS_PATH)
        mock_api.get(
            re.compile(f"^{re.escape(url)}\\?.*$"),
            body=json.dumps(
                {
                    "fills": [
                        {
                            "tradeId": "trade-match",
                            "orderId": "arcus-fill-poll",
                            "price": "50000",
                            "size": "0.001",
                            "fee": "0.05",
                            "createdAt": 1_700_000_000_000_000,
                        },
                        {
                            "tradeId": "trade-other",
                            "orderId": "different-order",
                            "price": "50000",
                            "size": "0.001",
                            "fee": "0",
                            "createdAt": 1_700_000_000_000_000,
                        },
                    ]
                }
            ),
        )

        await connector._update_orders_fills(
            orders=list(connector._order_tracker.all_fillable_orders.values())
        )
        fill_event = await asyncio.wait_for(listener.events.get(), timeout=1)

        order = connector._order_tracker.all_orders["hb-fill-poll"]
        if not isinstance(fill_event, OrderFilledEvent):
            self.fail("Expected REST fill reconciliation to emit an order-filled event.")
        self.assertEqual("trade-match", fill_event.exchange_trade_id)
        self.assertEqual(Decimal("0.001"), order.executed_amount_base)
        request = next(iter(mock_api.requests.values()))[0]
        self.assertEqual("BTC-USD", request.kwargs["params"]["market"])
        self.assertEqual(2, request.kwargs["params"]["accountIndex"])

    @aioresponses()
    async def test_status_polling_fetches_fill_history_once(self, mock_api):
        connector = self.create_authenticated_connector()
        connector._time_synchronizer.time = MagicMock(return_value=1_700_000_000.0)
        connector._set_current_timestamp(1_700_000_000.0)
        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": [self.btc_market()]}
        )
        connector.start_tracking_order(
            order_id="hb-poll",
            exchange_order_id="arcus-poll",
            trading_pair="BTC-USD",
            trade_type=TradeType.BUY,
            price=Decimal("50000"),
            amount=Decimal("0.002"),
            order_type=OrderType.LIMIT,
            position_action=PositionAction.OPEN,
        )

        account_url = web_utils.public_rest_url(CONSTANTS.ACCOUNT_PATH)
        positions_url = web_utils.public_rest_url(CONSTANTS.POSITIONS_PATH)
        fills_url = web_utils.public_rest_url(CONSTANTS.FILLS_PATH)
        order_url = web_utils.public_rest_url(f"{CONSTANTS.ORDER_PATH}/arcus-poll")
        mock_api.get(
            re.compile(f"^{re.escape(account_url)}\\?.*$"),
            body=json.dumps({"equity": "120", "freeCollateral": "80"}),
        )
        mock_api.get(re.compile(f"^{re.escape(positions_url)}\\?.*$"), body=json.dumps({"positions": {}}))
        mock_api.get(
            re.compile(f"^{re.escape(fills_url)}\\?.*$"),
            body=json.dumps({"fills": []}),
            repeat=True,
        )
        mock_api.get(
            re.compile(f"^{re.escape(order_url)}\\?.*$"),
            body=json.dumps(
                {
                    "order": {
                        "orderId": "arcus-poll",
                        "status": "OPEN",
                        "updatedAt": 1_700_000_000_000_000,
                    }
                }
            ),
        )

        await connector._status_polling_loop_fetch_updates()

        fills_requests = sum(
            len(calls)
            for (_, request_url), calls in mock_api.requests.items()
            if str(request_url).split("?", maxsplit=1)[0] == fills_url
        )
        self.assertEqual(1, fills_requests)

    @aioresponses()
    async def test_funding_payment_uses_mocked_rest_and_microsecond_timestamp(self, mock_api):
        connector = self.create_authenticated_connector()
        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": [self.btc_market()]}
        )
        url = web_utils.public_rest_url(CONSTANTS.FUNDING_PAYMENTS_PATH)
        mock_api.get(
            re.compile(f"^{re.escape(url)}\\?.*$"),
            body=json.dumps(
                {
                    "fundingPayments": [
                        {
                            "fundingRate": "-0.0001",
                            "payment": "1.25",
                            "time": 1_700_000_000_000_000,
                        }
                    ]
                }
            ),
        )

        timestamp, rate, payment = await connector._fetch_last_fee_payment("BTC-USD")

        self.assertEqual(1_700_000_000, timestamp)
        self.assertEqual(Decimal("-0.0001"), rate)
        self.assertEqual(Decimal("1.25"), payment)
        request = next(iter(mock_api.requests.values()))[0]
        self.assertEqual("BTC-USD", request.kwargs["params"]["market"])
        self.assertEqual(2, request.kwargs["params"]["accountIndex"])

    def test_get_fee_uses_configured_arcus_rate(self):
        connector = self.create_connector()

        fee = connector.get_fee(
            base_currency="BTC",
            quote_currency="USD",
            order_type=OrderType.LIMIT,
            order_side=TradeType.BUY,
            position_action=PositionAction.OPEN,
            amount=Decimal("0.001"),
            price=Decimal("50000"),
            is_maker=False,
        )

        self.assertEqual(Decimal("0.0005"), fee.percent)

    @aioresponses()
    async def test_set_leverage_uses_legacy_signature_body_without_timestamp_field(self, mock_api):
        connector = self.create_authenticated_connector()
        connector._get_market_info = AsyncMock(return_value=self.btc_market())
        connector._time_synchronizer.time = MagicMock(return_value=1_700_000_000.0)
        mock_api.post(
            web_utils.private_rest_url(CONSTANTS.SET_LEVERAGE_PATH),
            body=json.dumps({"status": "APPLIED"}),
        )

        success, message = await connector._set_trading_pair_leverage("BTC-USD", 5)

        request = next(iter(mock_api.requests.values()))[0]
        request_data = json.loads(request.kwargs["data"])
        self.assertTrue(success)
        self.assertEqual("", message)
        self.assertNotIn("timestamp", request_data)
        self.assertEqual(
            str(1_700_000_000_000_000_000),
            request.kwargs["headers"]["X-Timestamp"],
        )

    async def test_account_attribute_stream_updates_leverage_and_fee_schedule(self):
        connector = self.create_connector()
        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": [self.btc_market()]}
        )

        await connector._process_account_attribute_entry(
            {
                "type": "leverage",
                "marketId": 1,
                "marketDisplayName": "BTC-USD",
                "leverage": 7,
                "isolated": False,
            }
        )
        await connector._process_account_attribute_entry(
            {
                "type": "feeTier",
                "feeTierLevel": 2,
                "makerFeePpm": 200,
                "takerFeePpm": 500,
            }
        )

        self.assertEqual(7, connector.get_leverage("BTC-USD"))
        self.assertEqual(Decimal("0.0002"), connector._trading_fees["BTC-USD"].maker_percent_fee_decimal)
        self.assertEqual(Decimal("0.0005"), connector._trading_fees["BTC-USD"].taker_percent_fee_decimal)

    async def test_rejected_leverage_without_override_restores_market_default(self):
        connector = self.create_connector()
        connector._initialize_trading_pair_symbols_from_exchange_info(
            {"markets": [self.btc_market()]}
        )
        connector._get_market_info = AsyncMock(return_value=self.btc_market())

        await connector._process_account_attribute_entry(
            {
                "type": "leverageReject",
                "marketId": 1,
                "marketDisplayName": "BTC-USD",
                "leverage": 0,
                "status": "REJECTED",
                "rejectReason": "UNDERCOLLATERALIZED",
                "requestId": "request-1",
            }
        )

        self.assertEqual(10, connector.get_leverage("BTC-USD"))
