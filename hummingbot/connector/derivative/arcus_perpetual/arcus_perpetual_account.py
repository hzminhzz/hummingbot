import asyncio
from decimal import Decimal
from typing import Any, Dict, Optional

from hummingbot.connector.derivative.arcus_perpetual import arcus_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_context import ArcusPerpetualContext
from hummingbot.connector.derivative.position import Position
from hummingbot.core.data_type.common import PositionSide
from hummingbot.core.data_type.in_flight_order import OrderUpdate, TradeUpdate
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase, TradeFeeSchema


def account_query_params(self: ArcusPerpetualContext) -> Dict[str, Any]:
    connector = self
    if not connector._account_address:
        raise ValueError("Arcus account address is required for private account requests.")
    return {
        "address": connector._account_address.lower(),
        "accountIndex": connector._account_index,
    }


async def update_balances(self: ArcusPerpetualContext) -> None:
    connector = self
    response = await connector._api_get(
        path_url=CONSTANTS.ACCOUNT_PATH,
        params=account_query_params(connector),
        is_auth_required=True,
        limit_id=CONSTANTS.ACCOUNT_READ_LIMIT_ID,
    )
    account = response.get("account", response)
    connector._account_balances["USDG"] = Decimal(account["equity"])
    connector._account_available_balances["USDG"] = Decimal(account["freeCollateral"])
    for asset in set(connector._account_balances) - {"USDG"}:
        connector._account_balances.pop(asset, None)
        connector._account_available_balances.pop(asset, None)


async def position_from_market_data(
    self: ArcusPerpetualContext, position_data: Dict[str, Any]
) -> Optional[Position]:
    connector = self
    try:
        trading_pair = await connector.trading_pair_associated_to_exchange_symbol(
            position_data["marketDisplayName"]
        )
    except KeyError:
        return None
    signed_size = Decimal(position_data["size"])
    if signed_size == 0 or position_data.get("status") == "FLAT":
        for side in (PositionSide.LONG, PositionSide.SHORT):
            connector._perpetual_trading.remove_position(
                connector._perpetual_trading.position_key(trading_pair, side)
            )
        return None
    side = PositionSide.LONG if signed_size > 0 else PositionSide.SHORT
    return Position(
        trading_pair=trading_pair,
        position_side=side,
        unrealized_pnl=Decimal(position_data["unrealizedPnl"]),
        entry_price=Decimal(position_data["averageEntryPrice"]),
        amount=abs(signed_size),
        leverage=Decimal(position_data["leverage"]),
    )


async def update_positions(self: ArcusPerpetualContext) -> None:
    connector = self
    response = await connector._api_get(
        path_url=CONSTANTS.POSITIONS_PATH,
        params=account_query_params(connector),
        is_auth_required=True,
        limit_id=CONSTANTS.POSITIONS_LIMIT_ID,
    )
    connector._perpetual_trading._account_positions.clear()
    for position_data in response["positions"].values():
        position = await position_from_market_data(connector, position_data)
        if position is not None:
            position_key = connector._perpetual_trading.position_key(
                position.trading_pair, position.position_side
            )
            connector._perpetual_trading.set_position(position_key, position)


async def process_account_attribute_entry(
    self: ArcusPerpetualContext, entry: Dict[str, Any]
) -> None:
    connector = self
    entry_type = entry["type"]
    if entry_type in ("leverage", "leverageReject"):
        try:
            trading_pair = await connector.trading_pair_associated_to_exchange_symbol(
                entry["marketDisplayName"]
            )
        except KeyError:
            return
        leverage = int(entry["leverage"])
        if leverage == 0:
            market = await connector._get_market_info(trading_pair)
            leverage = int(Decimal("1") / Decimal(market["initialMarginFraction"]))
        connector._perpetual_trading.set_leverage(trading_pair, leverage)
    elif entry_type == "feeTier":
        schema = TradeFeeSchema(
            maker_percent_fee_decimal=Decimal(entry["makerFeePpm"]) / Decimal(1_000_000),
            taker_percent_fee_decimal=Decimal(entry["takerFeePpm"]) / Decimal(1_000_000),
        )
        for trading_pair in connector._trading_pairs:
            connector._trading_fees[trading_pair] = schema


def process_order_update(self: ArcusPerpetualContext, order_data: Dict[str, Any]) -> None:
    connector = self
    client_order_id = str(order_data.get("clientId", ""))
    exchange_order_id = str(order_data.get("orderId", ""))
    tracked_order = connector._order_tracker.all_updatable_orders.get(client_order_id)
    if tracked_order is None and exchange_order_id:
        tracked_order = connector._order_tracker.all_updatable_orders_by_exchange_order_id.get(exchange_order_id)
    if tracked_order is None:
        return
    state = order_data.get("status", order_data.get("state"))
    if state not in CONSTANTS.ORDER_STATE:
        return
    misc_updates = {}
    rejection_reason = order_data.get("rejectionReason")
    if rejection_reason:
        misc_updates["reason"] = rejection_reason
    update_timestamp = int(order_data.get("updatedAt", order_data.get("createdAt", 0))) * 1e-6
    if update_timestamp <= 0:
        update_timestamp = connector.current_timestamp
    connector._order_tracker.process_order_update(
        OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=update_timestamp,
            new_state=CONSTANTS.ORDER_STATE[state],
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id or "",
            misc_updates=misc_updates or None,
        )
    )


def process_user_fill(self: ArcusPerpetualContext, fill: Dict[str, Any]) -> None:
    connector = self
    client_order_id = str(fill.get("clientId", ""))
    exchange_order_id = str(fill.get("orderId", ""))
    tracked_order = connector._order_tracker.all_fillable_orders.get(client_order_id)
    if tracked_order is None and exchange_order_id:
        tracked_order = connector._order_tracker.all_fillable_orders_by_exchange_order_id.get(exchange_order_id)
    if tracked_order is None:
        return
    fill_size = Decimal(fill["size"])
    fill_price = Decimal(fill["price"])
    fee_amount = Decimal(fill["fee"])
    flat_fees = [] if fee_amount == 0 else [TokenAmount(amount=fee_amount, token="USDG")]
    fee = TradeFeeBase.new_perpetual_fee(
        fee_schema=connector.trade_fee_schema(),
        position_action=tracked_order.position,
        percent_token="USDG",
        flat_fees=flat_fees,
    )
    connector._order_tracker.process_trade_update(
        TradeUpdate(
            trade_id=str(fill["tradeId"]),
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id or "",
            trading_pair=tracked_order.trading_pair,
            fill_timestamp=int(fill["createdAt"]) * 1e-6,
            fill_price=fill_price,
            fill_base_amount=fill_size,
            fill_quote_amount=fill_size * fill_price,
            fee=fee,
        )
    )


async def listen_for_user_stream(self: ArcusPerpetualContext) -> None:
    connector = self
    async for event_message in connector._iter_user_event_queue():
        try:
            channel = event_message.get("channel")
            contents = event_message.get("contents", {})
            if not isinstance(contents, dict):
                continue
            if "equity" in contents and "freeCollateral" in contents:
                connector._account_balances["USDG"] = Decimal(contents["equity"])
                connector._account_available_balances["USDG"] = Decimal(contents["freeCollateral"])
            if channel == "orders":
                for order_data in contents.get("orders", [contents]):
                    process_order_update(connector, order_data)
            elif channel == "userFills":
                for fill in contents.get("fills", [contents]):
                    process_user_fill(connector, fill)
            elif channel == "positions":
                positions = contents.get("positions", {})
                if contents.get("isSnapshot"):
                    connector._perpetual_trading._account_positions.clear()
                if isinstance(positions, dict):
                    position_rows = positions.values()
                elif isinstance(positions, list):
                    position_rows = positions
                else:
                    position_rows = []
                for position_data in position_rows:
                    position = await position_from_market_data(connector, position_data)
                    if position is not None:
                        position_key = connector._perpetual_trading.position_key(
                            position.trading_pair, position.position_side
                        )
                        connector._perpetual_trading.set_position(position_key, position)
            elif channel == "accountAttributeUpdates":
                for entry in contents.get("entries", []):
                    await process_account_attribute_entry(connector, entry)
        except asyncio.CancelledError:
            raise
        except Exception:
            connector.logger().exception("Unexpected error processing Arcus user-stream event.")
