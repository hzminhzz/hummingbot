from decimal import Decimal
from typing import Tuple

from hummingbot.connector.derivative.arcus_perpetual import arcus_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_context import ArcusPerpetualContext
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_orders import signed_request_headers
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase


async def request_order_status(
    self: ArcusPerpetualContext, tracked_order: InFlightOrder
) -> OrderUpdate:
    connector = self
    response = await connector._api_get(
        path_url=f"{CONSTANTS.ORDER_PATH}/{tracked_order.exchange_order_id}",
        params=connector._account_query_params(),
        is_auth_required=True,
        limit_id=CONSTANTS.ORDER_STATUS_LIMIT_ID,
    )
    order = response.get("order", response)
    return OrderUpdate(
        trading_pair=tracked_order.trading_pair,
        update_timestamp=int(order["updatedAt"]) * 1e-6,
        new_state=CONSTANTS.ORDER_STATE[order["status"]],
        client_order_id=tracked_order.client_order_id,
        exchange_order_id=str(order["orderId"]),
    )


async def all_trade_updates_for_order(
    self: ArcusPerpetualContext, order: InFlightOrder
) -> list[TradeUpdate]:
    connector = self
    params = connector._account_query_params()
    params.update(
        {
            "market": await connector.exchange_symbol_associated_to_pair(order.trading_pair),
            "from": int(order.creation_timestamp * 1e6),
            "limit": 1000,
        }
    )
    response = await connector._api_get(
        path_url=CONSTANTS.FILLS_PATH,
        params=params,
        is_auth_required=True,
        limit_id=CONSTANTS.FILLS_LIMIT_ID,
    )
    updates = []
    for fill in response["fills"]:
        if str(fill["orderId"]) != str(order.exchange_order_id):
            continue
        fee_amount = Decimal(fill["fee"])
        flat_fees = [] if fee_amount == 0 else [TokenAmount(amount=fee_amount, token="USDG")]
        fee = TradeFeeBase.new_perpetual_fee(
            fee_schema=connector.trade_fee_schema(),
            position_action=order.position,
            percent_token="USDG",
            flat_fees=flat_fees,
        )
        fill_size = Decimal(fill["size"])
        fill_price = Decimal(fill["price"])
        updates.append(
            TradeUpdate(
                trade_id=str(fill["tradeId"]),
                client_order_id=order.client_order_id,
                exchange_order_id=str(fill["orderId"]),
                trading_pair=order.trading_pair,
                fill_timestamp=int(fill["createdAt"]) * 1e-6,
                fill_price=fill_price,
                fill_base_amount=fill_size,
                fill_quote_amount=fill_size * fill_price,
                fee=fee,
            )
        )
    return updates


async def set_trading_pair_leverage(
    self: ArcusPerpetualContext, trading_pair: str, leverage: int
) -> Tuple[bool, str]:
    connector = self
    market = await connector._get_market_info(trading_pair)
    timestamp_ns = int(connector._time_synchronizer.time() * 1e9)
    body = {
        "address": connector._account_query_params()["address"],
        "accountIndex": connector._account_index,
        "marketId": int(market["marketId"]),
        "leverage": leverage,
    }
    signature = connector._arcus_auth.sign_legacy_payload(timestamp_ns, "setLeverage", body)
    response = await connector._api_post(
        path_url=CONSTANTS.SET_LEVERAGE_PATH,
        data=body,
        headers=signed_request_headers(connector, timestamp_ns, signature),
        is_auth_required=True,
        limit_id=CONSTANTS.SET_LEVERAGE_LIMIT_ID,
        return_err=True,
    )
    if response.get("status") in ("APPLIED", "ACK"):
        return True, ""
    return False, str(response)


async def fetch_last_fee_payment(
    self: ArcusPerpetualContext, trading_pair: str
) -> Tuple[float, Decimal, Decimal]:
    connector = self
    params = connector._account_query_params()
    params["market"] = await connector.exchange_symbol_associated_to_pair(trading_pair)
    response = await connector._api_get(
        path_url=CONSTANTS.FUNDING_PAYMENTS_PATH,
        params=params,
        is_auth_required=True,
        limit_id=CONSTANTS.FUNDING_LIMIT_ID,
    )
    payments = response["fundingPayments"]
    if not payments:
        return 0, Decimal("-1"), Decimal("-1")
    payment = payments[0]
    return (
        int(payment["time"]) * 1e-6,
        Decimal(payment["fundingRate"]),
        Decimal(payment["payment"]),
    )
