from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Dict, Optional, Tuple

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.derivative.arcus_perpetual import arcus_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_context import ArcusPerpetualContext
from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder
from hummingbot.core.data_type.trade_fee import TradeFeeBase
from hummingbot.core.utils.estimate_fee import build_trade_fee


def signed_request_headers(
    self: ArcusPerpetualContext, timestamp_ns: int, signature: str
) -> Dict[str, str]:
    connector = self
    return connector._arcus_auth.rest_headers(timestamp_ns, signature)


def tick_size_for_price(market: Dict[str, Any], price: Decimal) -> Decimal:
    for tier in market["tickTiers"]:
        upper_price = tier.get("upToPrice")
        if upper_price is None or price <= Decimal(upper_price):
            return Decimal(tier["tick"])
    raise ValueError(f"No Arcus price tick tier applies to {price}.")


def quantize_price_to_tick(
    market: Dict[str, Any], price: Decimal, trade_type: TradeType
) -> Tuple[Decimal, int]:
    tier_tick = tick_size_for_price(market, price)
    rounding = ROUND_FLOOR if trade_type == TradeType.BUY else ROUND_CEILING
    tier_ticks = int((price / tier_tick).to_integral_value(rounding=rounding))
    quantized_price = tier_tick * tier_ticks
    base_tick = Decimal(market["tickSize"])
    base_ticks = quantized_price / base_tick
    if base_ticks != base_ticks.to_integral_value():
        raise ValueError(f"Arcus tier price {quantized_price} is not aligned to base tick {base_tick}.")
    price_ticks = int(base_ticks)
    if price_ticks <= 0:
        raise ValueError(f"Arcus price must be positive: {price}.")
    return quantized_price, price_ticks


def quantity_quantums(market: Dict[str, Any], amount: Decimal) -> int:
    step_size = Decimal(market["stepSize"])
    quantums = amount / step_size
    if quantums != quantums.to_integral_value():
        raise ValueError(f"Arcus quantity {amount} is not a multiple of step size {step_size}.")
    quantity = int(quantums)
    if quantity <= 0:
        raise ValueError(f"Arcus quantity must be positive: {amount}.")
    return quantity


def good_til_time_us(self: ArcusPerpetualContext) -> int:
    connector = self
    return int(connector._time_synchronizer.time() * 1e6) + 40 * 24 * 60 * 60 * 1_000_000


def get_fee(
    self: ArcusPerpetualContext,
    base_currency: str,
    quote_currency: str,
    order_type: OrderType,
    order_side: TradeType,
    position_action: PositionAction,
    amount: Decimal,
    price: Decimal = s_decimal_NaN,
    is_maker: Optional[bool] = None,
) -> TradeFeeBase:
    connector = self
    return build_trade_fee(
        connector.name,
        is_maker or False,
        base_currency=base_currency,
        quote_currency=quote_currency,
        order_type=order_type,
        order_side=order_side,
        amount=amount,
        price=price,
    )


async def place_order(
    self: ArcusPerpetualContext,
    order_id: str,
    trading_pair: str,
    amount: Decimal,
    trade_type: TradeType,
    order_type: OrderType,
    price: Decimal,
    position_action: PositionAction = PositionAction.NIL,
    **kwargs: Any,
) -> Tuple[str, float]:
    connector = self
    market = await connector._get_market_info(trading_pair)
    timestamp_ns = int(connector._time_synchronizer.time() * 1e9)
    good_til_us = good_til_time_us(connector)
    if order_type == OrderType.MARKET:
        mark_price = Decimal(market["markPrice"])
        price = mark_price * (Decimal("1.05") if trade_type == TradeType.BUY else Decimal("0.95"))
    price, price_ticks = quantize_price_to_tick(market, price, trade_type)
    quantity_count = quantity_quantums(market, amount)
    tif = {
        OrderType.LIMIT: ("GTT", 0),
        OrderType.LIMIT_MAKER: ("ALO", 3),
        OrderType.MARKET: ("IOC", 2),
    }[order_type]
    order_side = "BUY" if trade_type == TradeType.BUY else "SELL"
    reduce_only = position_action == PositionAction.CLOSE
    if amount > Decimal(market["maxOrderSize"]):
        raise ValueError(
            f"Arcus order size {amount} exceeds {trading_pair} maximum {market['maxOrderSize']}."
        )
    if not reduce_only and amount * price < Decimal(market["minOrderNotional"]):
        raise ValueError(
            f"Arcus order notional {amount * price} is below minimum {market['minOrderNotional']}."
        )
    address = connector._account_query_params()["address"]
    market_id = int(market["marketId"])
    payload = {
        "ad": address,
        "ai": connector._account_index,
        "c": order_id,
        "ct": timestamp_ns,
        "g": good_til_us * 1000,
        "m": market_id,
        "op": 1,
        "p": price_ticks,
        "q": quantity_count,
        "r": int(reduce_only),
        "s": 0 if trade_type == TradeType.BUY else 1,
        "t": tif[1],
        "v": 1,
    }
    signature = connector._arcus_auth.sign_typed_payload(payload)
    response = await connector._api_post(
        path_url=CONSTANTS.PLACE_ORDER_PATH,
        data={
            "address": address,
            "accountIndex": connector._account_index,
            "marketId": market_id,
            "clientId": order_id,
            "orderSide": order_side,
            "orderType": "MARKET" if order_type == OrderType.MARKET else "LIMIT",
            "timeInForce": tif[0],
            "quantity": format(amount, "f"),
            "price": format(price, "f"),
            "reduceOnly": reduce_only,
            "goodTilTime": str(good_til_us),
            "timestamp": timestamp_ns,
        },
        headers=signed_request_headers(connector, timestamp_ns, signature),
        is_auth_required=True,
        limit_id=CONSTANTS.PLACE_ORDER_LIMIT_ID,
    )
    return str(response["orderId"]), connector.current_timestamp


async def place_cancel(
    self: ArcusPerpetualContext, order_id: str, tracked_order: InFlightOrder
) -> bool:
    connector = self
    market = await connector._get_market_info(tracked_order.trading_pair)
    timestamp_ns = int(connector._time_synchronizer.time() * 1e9)
    address = connector._account_query_params()["address"]
    exchange_order_id = tracked_order.exchange_order_id
    payload: Dict[str, Any] = {
        "ad": address,
        "ai": connector._account_index,
        "ct": timestamp_ns,
        "m": int(market["marketId"]),
        "op": 2,
        "v": 1,
    }
    data: Dict[str, Any] = {
        "address": address,
        "accountIndex": connector._account_index,
        "marketId": int(market["marketId"]),
        "timestamp": timestamp_ns,
    }
    if exchange_order_id is None:
        payload["c"] = order_id
        data.update(kind="clientId", clientId=order_id)
    else:
        payload["id"] = str(exchange_order_id)
        data.update(kind="orderId", orderId=str(exchange_order_id))
    signature = connector._arcus_auth.sign_typed_payload(payload)
    await connector._api_post(
        path_url=CONSTANTS.CANCEL_ORDER_PATH,
        data=data,
        headers=signed_request_headers(connector, timestamp_ns, signature),
        is_auth_required=True,
        limit_id=CONSTANTS.CANCEL_ORDER_LIMIT_ID,
    )
    return True
