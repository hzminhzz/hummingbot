import asyncio
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol

import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_constants as CONSTANTS
import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_web_utils as web_utils
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.funding_info import FundingInfo, FundingInfoUpdate
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.perpetual_api_order_book_data_source import PerpetualAPIOrderBookDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant


class ArcusPerpetualConnector(Protocol):
    async def exchange_symbol_associated_to_pair(self, trading_pair: str) -> str:
        ...

    async def trading_pair_associated_to_exchange_symbol(self, symbol: str) -> str:
        ...

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        ...

    async def _get_market_info(self, trading_pair: str) -> Dict[str, Any]:
        ...


class ArcusPerpetualAPIOrderBookDataSource(PerpetualAPIOrderBookDataSource):
    _FUNDING_INFO_POLL_INTERVAL = 60

    def __init__(
        self,
        trading_pairs: List[str],
        connector: ArcusPerpetualConnector,
        api_factory: WebAssistantsFactory,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ) -> None:
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._last_sequence_by_pair: Dict[str, int] = {}
        self._first_diff_after_snapshot: set[str] = set()
        self._awaiting_snapshot: set[str] = set()
        self._snapshot_ready_by_pair = {
            trading_pair: asyncio.Event() for trading_pair in trading_pairs
        }

    async def get_last_traded_prices(
        self, trading_pairs: List[str], domain: Optional[str] = None
    ) -> Dict[str, float]:
        return {
            trading_pair: await self._connector._get_last_traded_price(trading_pair)
            for trading_pair in trading_pairs
        }

    async def get_funding_info(self, trading_pair: str) -> FundingInfo:
        market = await self._connector._get_market_info(trading_pair)
        return FundingInfo(
            trading_pair=trading_pair,
            index_price=Decimal(market["oraclePrice"]),
            mark_price=Decimal(market["markPrice"]),
            next_funding_utc_timestamp=int(market["nextFundingAt"]),
            rate=Decimal(market["fundingRate"]),
        )

    async def listen_for_funding_info(self, output: asyncio.Queue[Any]):
        while True:
            try:
                for trading_pair in self._trading_pairs:
                    funding_info = await self.get_funding_info(trading_pair)
                    output.put_nowait(
                        FundingInfoUpdate(
                            trading_pair=trading_pair,
                            index_price=funding_info.index_price,
                            mark_price=funding_info.mark_price,
                            next_funding_utc_timestamp=funding_info.next_funding_utc_timestamp,
                            rate=funding_info.rate,
                        )
                    )
                await self._sleep(self._FUNDING_INFO_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception("Unexpected error updating Arcus funding information.")
                await self._sleep(self._FUNDING_INFO_POLL_INTERVAL)

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        market = await self._connector.exchange_symbol_associated_to_pair(trading_pair)
        rest_assistant = await self._api_factory.get_rest_assistant()
        response = await rest_assistant.execute_request(
            url=web_utils.public_rest_url(f"{CONSTANTS.BOOK_PATH}/{market}", self._domain),
            params={"nLevels": "100"},
            method=RESTMethod.GET,
            throttler_limit_id=CONSTANTS.ORDERBOOK_LIMIT_ID,
        )
        if not isinstance(response, dict):
            raise IOError(f"Unexpected Arcus order book response: {response}")
        snapshot = self._snapshot_message(trading_pair, response)
        previous_sequence_id = self._last_sequence_by_pair.get(trading_pair)
        if previous_sequence_id is None or snapshot.update_id > previous_sequence_id:
            self._last_sequence_by_pair[trading_pair] = snapshot.update_id
            self._first_diff_after_snapshot.add(trading_pair)
            self._snapshot_ready_by_pair.setdefault(trading_pair, asyncio.Event()).set()
        return snapshot

    @staticmethod
    def _snapshot_message(trading_pair: str, contents: Dict[str, Any]) -> OrderBookMessage:
        return OrderBookMessage(
            OrderBookMessageType.SNAPSHOT,
            {
                "trading_pair": trading_pair,
                "update_id": int(contents["lastSequenceId"]),
                "bids": contents["bids"],
                "asks": contents["asks"],
            },
            timestamp=int(contents["timestamp"]) * 1e-6,
        )

    async def _connected_websocket_assistant(self) -> WSAssistant:
        for snapshot_ready in self._snapshot_ready_by_pair.values():
            snapshot_ready.clear()
        ws = await self._api_factory.get_ws_assistant()
        await ws.connect(
            ws_url=web_utils.public_ws_url(self._domain),
            ws_headers={"User-Agent": CONSTANTS.USER_AGENT},
            ping_timeout=30,
        )
        return ws

    async def _subscribe_channels(self, ws: WSAssistant):
        for trading_pair in self._trading_pairs:
            market = await self._connector.exchange_symbol_associated_to_pair(trading_pair)
            for channel in ("l2OrderbookUpdates", "trades"):
                await ws.send(
                    WSJSONRequest(
                        {
                            "type": "subscribe",
                            "channel": channel,
                            "id": market,
                        }
                    )
                )

    async def subscribe_to_trading_pair(self, trading_pair: str) -> bool:
        if self._ws_assistant is None:
            return False
        self._snapshot_ready_by_pair.setdefault(trading_pair, asyncio.Event()).clear()
        market = await self._connector.exchange_symbol_associated_to_pair(trading_pair)
        for channel in ("l2OrderbookUpdates", "trades"):
            await self._ws_assistant.send(
                WSJSONRequest({"type": "subscribe", "channel": channel, "id": market})
            )
        self.add_trading_pair(trading_pair)
        return True

    async def unsubscribe_from_trading_pair(self, trading_pair: str) -> bool:
        if self._ws_assistant is None:
            return False
        market = await self._connector.exchange_symbol_associated_to_pair(trading_pair)
        for channel in ("l2OrderbookUpdates", "trades"):
            await self._ws_assistant.send(
                WSJSONRequest({"type": "unsubscribe", "channel": channel, "id": market})
            )
        self.remove_trading_pair(trading_pair)
        self._last_sequence_by_pair.pop(trading_pair, None)
        self._awaiting_snapshot.discard(trading_pair)
        self._snapshot_ready_by_pair.pop(trading_pair, None)
        return True

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        channel = event_message.get("channel")
        message_type = event_message.get("type")
        if channel == "l2OrderbookUpdates" and message_type == "subscribed":
            return self._snapshot_messages_queue_key
        if channel == "l2OrderbookUpdates" and message_type == "channel_data":
            return self._diff_messages_queue_key
        if channel == "trades" and message_type == "channel_data":
            return self._trade_messages_queue_key
        return ""

    async def _parse_order_book_snapshot_message(
        self, raw_message: Dict[str, Any], message_queue: asyncio.Queue[Any]
    ):
        if raw_message.get("type") == "resync":
            trading_pair = raw_message["trading_pair"]
            snapshot = await self._order_book_snapshot(trading_pair)
            self._awaiting_snapshot.discard(trading_pair)
            message_queue.put_nowait(snapshot)
            return
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(raw_message["id"])
        contents = raw_message["contents"]
        sequence_id = int(contents["lastSequenceId"])
        previous_sequence_id = self._last_sequence_by_pair.get(trading_pair)
        if previous_sequence_id is not None and sequence_id < previous_sequence_id:
            return
        self._last_sequence_by_pair[trading_pair] = sequence_id
        self._first_diff_after_snapshot.add(trading_pair)
        self._awaiting_snapshot.discard(trading_pair)
        self._snapshot_ready_by_pair.setdefault(trading_pair, asyncio.Event()).set()
        message_queue.put_nowait(self._snapshot_message(trading_pair, contents))

    async def _parse_order_book_diff_message(
        self, raw_message: Dict[str, Any], message_queue: asyncio.Queue[Any]
    ):
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(raw_message["id"])
        contents = raw_message["contents"]
        sequence_id = int(contents["lastSequenceId"])
        snapshot_ready = self._snapshot_ready_by_pair.setdefault(trading_pair, asyncio.Event())
        if trading_pair not in self._last_sequence_by_pair:
            await snapshot_ready.wait()
        previous_sequence_id = self._last_sequence_by_pair.get(trading_pair)
        if trading_pair in self._awaiting_snapshot:
            return
        if previous_sequence_id is not None and sequence_id <= previous_sequence_id:
            return
        initial_snapshot_gap = trading_pair in self._first_diff_after_snapshot
        if previous_sequence_id is not None and not initial_snapshot_gap and sequence_id != previous_sequence_id + 1:
            self.logger().warning(
                f"Arcus order book sequence gap for {trading_pair}: "
                f"expected {previous_sequence_id + 1}, received {sequence_id}; requesting snapshot."
            )
            self._awaiting_snapshot.add(trading_pair)
            self._message_queue[self._snapshot_messages_queue_key].put_nowait(
                {"type": "resync", "trading_pair": trading_pair}
            )
            return
        self._first_diff_after_snapshot.discard(trading_pair)
        self._last_sequence_by_pair[trading_pair] = sequence_id
        timestamp_us = contents.get("timestamp")
        timestamp = int(timestamp_us) * 1e-6 if timestamp_us is not None else self._time()
        message_queue.put_nowait(
            OrderBookMessage(
                OrderBookMessageType.DIFF,
                {
                    "trading_pair": trading_pair,
                    "update_id": sequence_id,
                    "bids": contents["bids"],
                    "asks": contents["asks"],
                },
                timestamp=timestamp,
            )
        )

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue[Any]):
        for trade in raw_message["contents"]:
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
                trade["marketDisplayName"]
            )
            trade_type = TradeType.BUY if trade["side"] == "BUY" else TradeType.SELL
            message_queue.put_nowait(
                OrderBookMessage(
                    OrderBookMessageType.TRADE,
                    {
                        "trading_pair": trading_pair,
                        "trade_type": float(trade_type.value),
                        "trade_id": trade["tradeId"],
                        "update_id": trade["sequenceNumber"],
                        "price": trade["price"],
                        "amount": trade["size"],
                    },
                    timestamp=int(trade["timestamp"]) * 1e-6,
                )
            )

    async def _parse_funding_info_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue[Any]):
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(raw_message["id"])
        funding_info = await self.get_funding_info(trading_pair)
        message_queue.put_nowait(
            FundingInfoUpdate(
                trading_pair=trading_pair,
                index_price=funding_info.index_price,
                mark_price=funding_info.mark_price,
                next_funding_utc_timestamp=funding_info.next_funding_utc_timestamp,
                rate=funding_info.rate,
            )
        )
