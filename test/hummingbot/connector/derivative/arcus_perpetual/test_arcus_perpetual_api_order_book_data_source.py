import asyncio
import json
import re
from decimal import Decimal
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from aioresponses import aioresponses

import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_constants as CONSTANTS
import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_web_utils as web_utils
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_api_order_book_data_source import (
    ArcusPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.core.data_type.funding_info import FundingInfoUpdate
from hummingbot.core.data_type.order_book_message import OrderBookMessageType


class ArcusPerpetualAPIOrderBookDataSourceTests(IsolatedAsyncioTestCase):
    def create_data_source(self) -> ArcusPerpetualAPIOrderBookDataSource:
        connector = MagicMock()
        connector.exchange_symbol_associated_to_exchange_symbol = AsyncMock(return_value="BTC-USD")
        connector.trading_pair_associated_to_exchange_symbol = AsyncMock(return_value="BTC-USD")
        connector.exchange_symbol_associated_to_pair = AsyncMock(return_value="BTC-USD")
        connector._get_last_traded_price = AsyncMock(return_value=50_000.0)
        connector._get_market_info = AsyncMock()
        return ArcusPerpetualAPIOrderBookDataSource(
            trading_pairs=["BTC-USD"],
            connector=connector,
            api_factory=MagicMock(),
            domain=CONSTANTS.DEFAULT_DOMAIN,
        )

    @aioresponses()
    async def test_order_book_snapshot_fetches_and_records_rest_sequence(self, mock_api):
        data_source = self.create_data_source()
        data_source._api_factory = web_utils.build_api_factory()
        url = web_utils.public_rest_url(f"{CONSTANTS.BOOK_PATH}/BTC-USD")
        mock_api.get(
            re.compile(f"^{re.escape(url)}\\?nLevels=100$"),
            body=json.dumps(
                {
                    "bids": [["50000", "1"]],
                    "asks": [["50001", "2"]],
                    "lastSequenceId": 100,
                    "timestamp": 1_700_000_000_000_000,
                }
            ),
        )

        snapshot = await data_source._order_book_snapshot("BTC-USD")

        self.assertEqual(OrderBookMessageType.SNAPSHOT, snapshot.type)
        self.assertEqual(100, snapshot.update_id)
        self.assertEqual(100, data_source._last_sequence_by_pair["BTC-USD"])
        self.assertTrue(data_source._snapshot_ready_by_pair["BTC-USD"].is_set())

    async def test_diff_waits_for_snapshot_before_applying_spliced_sequence(self):
        data_source = self.create_data_source()
        output = asyncio.Queue()
        diff_task = asyncio.create_task(
            data_source._parse_order_book_diff_message(
                {
                    "id": "BTC-USD",
                    "contents": {
                        "bids": [["50000", "0.5"]],
                        "asks": [],
                        "lastSequenceId": 104,
                        "timestamp": 1_700_000_000_000_100,
                    },
                },
                output,
            )
        )
        snapshot_task = asyncio.create_task(
            data_source._parse_order_book_snapshot_message(
                {
                    "id": "BTC-USD",
                    "contents": {
                        "bids": [["50000", "1"]],
                        "asks": [["50001", "2"]],
                        "lastSequenceId": 100,
                        "timestamp": 1_700_000_000_000_000,
                    },
                },
                output,
            )
        )

        await asyncio.gather(snapshot_task, diff_task)

        snapshot = output.get_nowait()
        diff = output.get_nowait()
        self.assertEqual(OrderBookMessageType.SNAPSHOT, snapshot.type)
        self.assertEqual(OrderBookMessageType.DIFF, diff.type)
        self.assertEqual(104, diff.update_id)

    async def test_accepts_initial_sequence_splice_then_resyncs_midstream_gap(self):
        data_source = self.create_data_source()
        output = asyncio.Queue()
        await data_source._parse_order_book_snapshot_message(
            {
                "id": "BTC-USD",
                "contents": {
                    "bids": [["50000", "1"]],
                    "asks": [["50001", "2"]],
                    "lastSequenceId": 100,
                    "timestamp": 1_700_000_000_000_000,
                },
            },
            output,
        )
        snapshot = output.get_nowait()
        self.assertEqual(OrderBookMessageType.SNAPSHOT, snapshot.type)
        self.assertEqual(100, snapshot.update_id)
        self.assertEqual(1_700_000_000, snapshot.timestamp)

        await data_source._parse_order_book_diff_message(
            {
                "id": "BTC-USD",
                "contents": {
                    "bids": [["50000", "0.5"]],
                    "asks": [],
                    "lastSequenceId": 104,
                    "timestamp": 1_700_000_000_000_100,
                },
            },
            output,
        )
        self.assertEqual(104, output.get_nowait().update_id)

        await data_source._parse_order_book_diff_message(
            {
                "id": "BTC-USD",
                "contents": {
                    "bids": [],
                    "asks": [["50001", "0"]],
                    "lastSequenceId": 106,
                    "timestamp": 1_700_000_000_000_200,
                },
            },
            output,
        )
        self.assertTrue(data_source._awaiting_snapshot)
        resync = data_source._message_queue[data_source._snapshot_messages_queue_key].get_nowait()
        self.assertEqual({"type": "resync", "trading_pair": "BTC-USD"}, resync)

    async def test_diff_without_server_timestamp_uses_local_receive_time(self):
        data_source = self.create_data_source()
        data_source._time = MagicMock(return_value=1_700_000_000.25)
        output = asyncio.Queue()
        await data_source._parse_order_book_snapshot_message(
            {
                "id": "BTC-USD",
                "contents": {
                    "bids": [["50000", "1"]],
                    "asks": [["50001", "2"]],
                    "lastSequenceId": 100,
                    "timestamp": 1_700_000_000_000_000,
                },
            },
            output,
        )
        output.get_nowait()

        await data_source._parse_order_book_diff_message(
            {
                "id": "BTC-USD",
                "contents": {
                    "bids": [["50000", "0.5"]],
                    "asks": [],
                    "lastSequenceId": 101,
                },
            },
            output,
        )

        self.assertEqual(1_700_000_000.25, output.get_nowait().timestamp)

    async def test_trade_frame_emits_one_message_per_fill(self):
        data_source = self.create_data_source()
        output = asyncio.Queue()
        await data_source._parse_trade_message(
            {
                "contents": [
                    {
                        "marketDisplayName": "BTC-USD",
                        "side": "BUY",
                        "price": "50000.5",
                        "size": "0.02",
                        "tradeId": "f-1",
                        "sequenceNumber": 45,
                        "timestamp": 1_700_000_000_000_000,
                    },
                    {
                        "marketDisplayName": "BTC-USD",
                        "side": "SELL",
                        "price": "50000.4",
                        "size": "0.01",
                        "tradeId": "f-2",
                        "sequenceNumber": 46,
                        "timestamp": 1_700_000_000_000_100,
                    },
                ]
            },
            output,
        )

        first = output.get_nowait()
        second = output.get_nowait()
        self.assertEqual(OrderBookMessageType.TRADE, first.type)
        self.assertEqual("f-1", first.content["trade_id"])
        self.assertEqual("50000.5", first.content["price"])
        self.assertEqual("0.02", first.content["amount"])
        self.assertEqual(1_700_000_000, first.timestamp)
        self.assertEqual("f-2", second.content["trade_id"])

    async def test_funding_poll_emits_market_mark_oracle_and_rate(self):
        data_source = self.create_data_source()
        data_source._connector._get_market_info = AsyncMock(
            return_value={
                "oraclePrice": "50000",
                "markPrice": "50002",
                "nextFundingAt": 1_700_003_600,
                "fundingRate": "0.0001",
            }
        )
        data_source._sleep = AsyncMock(side_effect=asyncio.CancelledError)
        output = asyncio.Queue()

        with self.assertRaises(asyncio.CancelledError):
            await data_source.listen_for_funding_info(output)

        update = output.get_nowait()
        self.assertIsInstance(update, FundingInfoUpdate)
        self.assertEqual("BTC-USD", update.trading_pair)
        self.assertEqual(Decimal("50000"), update.index_price)
        self.assertEqual(Decimal("50002"), update.mark_price)
        self.assertEqual(Decimal("0.0001"), update.rate)

    async def test_last_traded_prices_fetch_each_requested_pair(self):
        data_source = self.create_data_source()
        data_source._connector._get_last_traded_price = AsyncMock(side_effect=[50_000.0, 2_500.0])

        prices = await data_source.get_last_traded_prices(["BTC-USD", "ETH-USD"])

        self.assertEqual({"BTC-USD": 50_000.0, "ETH-USD": 2_500.0}, prices)
        self.assertEqual(["BTC-USD", "ETH-USD"], [
            call.args[0] for call in data_source._connector._get_last_traded_price.await_args_list
        ])

    async def test_funding_channel_parser_emits_current_funding_info(self):
        data_source = self.create_data_source()
        data_source._connector._get_market_info = AsyncMock(
            return_value={
                "oraclePrice": "50000",
                "markPrice": "50002",
                "nextFundingAt": 1_700_003_600,
                "fundingRate": "0.0001",
            }
        )
        output = asyncio.Queue()

        await data_source._parse_funding_info_message({"id": "BTC-USD"}, output)

        update = output.get_nowait()
        self.assertEqual("BTC-USD", update.trading_pair)
        self.assertEqual(Decimal("50000"), update.index_price)
        self.assertEqual(Decimal("50002"), update.mark_price)
        self.assertEqual(1_700_003_600, update.next_funding_utc_timestamp)
        self.assertEqual(Decimal("0.0001"), update.rate)

    async def test_public_websocket_connection_uses_required_user_agent(self):
        data_source = self.create_data_source()
        websocket = MagicMock()
        websocket.connect = AsyncMock()
        api_factory = MagicMock()
        api_factory.get_ws_assistant = AsyncMock(return_value=websocket)
        data_source._api_factory = api_factory

        connected = await data_source._connected_websocket_assistant()

        self.assertIs(websocket, connected)
        self.assertEqual(
            "OpenAI File Downloader, XaiImageApiFetch/1.0",
            websocket.connect.await_args.kwargs["ws_headers"]["User-Agent"],
        )

    async def test_dynamic_pair_methods_return_false_without_a_websocket(self):
        data_source = self.create_data_source()

        self.assertFalse(await data_source.subscribe_to_trading_pair("ETH-USD"))
        self.assertFalse(await data_source.unsubscribe_from_trading_pair("ETH-USD"))


class ArcusPerpetualNetworkMockingAssistantTests(IsolatedAsyncioTestCase):
    mocking_assistant: NetworkMockingAssistant | None = None

    async def asyncSetUp(self) -> None:
        self.mocking_assistant = NetworkMockingAssistant()

    async def test_websocket_routes_and_parses_mocked_snapshot_and_delta(self):
        assert self.mocking_assistant is not None
        mocking_assistant = self.mocking_assistant
        api_factory = web_utils.build_api_factory()
        websocket_mock = mocking_assistant.configure_web_assistants_factory(api_factory)
        connector = MagicMock()
        connector.exchange_symbol_associated_to_pair = AsyncMock(return_value="BTC-USD")
        connector.trading_pair_associated_to_exchange_symbol = AsyncMock(return_value="BTC-USD")
        data_source = ArcusPerpetualAPIOrderBookDataSource(
            trading_pairs=["BTC-USD"],
            connector=connector,
            api_factory=api_factory,
        )

        websocket = await data_source._connected_websocket_assistant()
        await data_source._subscribe_channels(websocket)
        sent_subscriptions = mocking_assistant.json_messages_sent_through_websocket(websocket_mock)
        self.assertEqual(
            [
                {"type": "subscribe", "channel": "l2OrderbookUpdates", "id": "BTC-USD"},
                {"type": "subscribe", "channel": "trades", "id": "BTC-USD"},
            ],
            sent_subscriptions,
        )

        listener_task = asyncio.create_task(data_source._process_websocket_messages(websocket))
        snapshot_frame = {
            "type": "subscribed",
            "channel": "l2OrderbookUpdates",
            "id": "BTC-USD",
            "contents": {
                "bids": [["50000", "1"]],
                "asks": [["50001", "2"]],
                "lastSequenceId": 100,
                "timestamp": 1_700_000_000_000_000,
            },
        }
        snapshot_output = asyncio.Queue()
        mocking_assistant.add_websocket_aiohttp_message(websocket_mock, json.dumps(snapshot_frame))
        routed_snapshot = await asyncio.wait_for(
            data_source._message_queue[data_source._snapshot_messages_queue_key].get(),
            timeout=1,
        )
        await data_source._parse_order_book_snapshot_message(routed_snapshot, snapshot_output)
        snapshot = snapshot_output.get_nowait()
        self.assertEqual(OrderBookMessageType.SNAPSHOT, snapshot.type)
        self.assertEqual(100, snapshot.update_id)

        delta_frame = {
            "type": "channel_data",
            "channel": "l2OrderbookUpdates",
            "id": "BTC-USD",
            "contents": {
                "bids": [["50000", "0.5"]],
                "asks": [],
                "lastSequenceId": 101,
                "timestamp": 1_700_000_000_000_100,
            },
        }
        delta_output = asyncio.Queue()
        mocking_assistant.add_websocket_aiohttp_message(websocket_mock, json.dumps(delta_frame))
        routed_delta = await asyncio.wait_for(
            data_source._message_queue[data_source._diff_messages_queue_key].get(),
            timeout=1,
        )
        await data_source._parse_order_book_diff_message(routed_delta, delta_output)
        delta = delta_output.get_nowait()
        self.assertEqual(OrderBookMessageType.DIFF, delta.type)
        self.assertEqual(101, delta.update_id)

        listener_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await listener_task
        await websocket.disconnect()

    async def test_dynamic_pair_subscription_and_unsubscription_use_mocked_socket(self):
        assert self.mocking_assistant is not None
        mocking_assistant = self.mocking_assistant
        api_factory = web_utils.build_api_factory()
        websocket_mock = mocking_assistant.configure_web_assistants_factory(api_factory)
        connector = MagicMock()
        connector.exchange_symbol_associated_to_pair = AsyncMock(side_effect=lambda pair: pair)
        data_source = ArcusPerpetualAPIOrderBookDataSource(
            trading_pairs=["BTC-USD"],
            connector=connector,
            api_factory=api_factory,
        )

        websocket = await data_source._connected_websocket_assistant()
        data_source._ws_assistant = websocket
        try:
            self.assertTrue(await data_source.subscribe_to_trading_pair("ETH-USD"))
            self.assertTrue(await data_source.unsubscribe_from_trading_pair("ETH-USD"))

            self.assertEqual(
                [
                    {"type": "subscribe", "channel": "l2OrderbookUpdates", "id": "ETH-USD"},
                    {"type": "subscribe", "channel": "trades", "id": "ETH-USD"},
                    {"type": "unsubscribe", "channel": "l2OrderbookUpdates", "id": "ETH-USD"},
                    {"type": "unsubscribe", "channel": "trades", "id": "ETH-USD"},
                ],
                mocking_assistant.json_messages_sent_through_websocket(websocket_mock),
            )
            self.assertNotIn("ETH-USD", data_source._trading_pairs)
        finally:
            await websocket.disconnect()
