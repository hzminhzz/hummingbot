from decimal import Decimal
from typing import Any, Dict, List

from bidict import bidict

from hummingbot.connector.derivative.arcus_perpetual import arcus_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_context import ArcusPerpetualContext
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair


async def get_markets(
    self: ArcusPerpetualContext, refresh: bool = False
) -> Dict[str, Dict[str, Any]]:
    connector = self
    if (
        not refresh
        and connector._market_info
        and connector._time() - connector._market_info_last_update < 30
    ):
        return connector._market_info
    async with connector._market_info_lock:
        if (
            not refresh
            and connector._market_info
            and connector._time() - connector._market_info_last_update < 30
        ):
            return connector._market_info
        response = await connector._api_get(
            path_url=CONSTANTS.MARKETS_PATH,
            limit_id=CONSTANTS.MARKETS_LIMIT_ID,
        )
        if "markets" not in response:
            raise IOError(f"Unexpected Arcus markets response: {response}")
        connector._market_info = {
            market["marketDisplayName"]: market
            for market in response["markets"]
            if market["type"] == "PERPETUAL"
        }
        connector._market_info_last_update = connector._time()
        return connector._market_info


async def get_market_info(
    self: ArcusPerpetualContext, trading_pair: str
) -> Dict[str, Any]:
    connector = self
    symbol = await connector.exchange_symbol_associated_to_pair(trading_pair)
    return (await get_markets(connector))[symbol]


async def get_last_traded_price(
    self: ArcusPerpetualContext, trading_pair: str
) -> float:
    connector = self
    market = await get_market_info(connector, trading_pair)
    return float(market["lastTradePrice"])


async def update_trading_rules(self: ArcusPerpetualContext) -> None:
    connector = self
    markets = await get_markets(connector, refresh=True)
    initialize_symbols(connector, {"markets": list(markets.values())})
    rules = await format_trading_rules(connector, {"markets": list(markets.values())})
    connector._trading_rules.clear()
    for rule in rules:
        connector._trading_rules[rule.trading_pair] = rule
        market = markets[rule.trading_pair]
        max_leverage = int(Decimal("1") / Decimal(market["initialMarginFraction"]))
        connector._perpetual_trading.set_leverage(rule.trading_pair, max_leverage)


async def format_trading_rules(
    self: ArcusPerpetualContext, exchange_info_dict: Dict[str, Any]
) -> List[TradingRule]:
    exchange_info = exchange_info_dict
    rules = []
    for market in exchange_info["markets"]:
        if market["type"] != "PERPETUAL" or market["status"] != "ONLINE":
            continue
        trading_pair = combine_to_hb_trading_pair(market["baseAsset"], market["quoteAsset"])
        rules.append(
            TradingRule(
                trading_pair=trading_pair,
                min_order_size=Decimal(market["minOrderSize"]),
                max_order_size=Decimal(market["maxOrderSize"]),
                min_price_increment=Decimal(market["tickSize"]),
                min_base_amount_increment=Decimal(market["stepSize"]),
                min_notional_size=Decimal(market["minOrderNotional"]),
                buy_order_collateral_token="USDG",
                sell_order_collateral_token="USDG",
            )
        )
    return rules


def initialize_symbols(self: ArcusPerpetualContext, exchange_info: Dict[str, Any]) -> None:
    connector = self
    mapping: bidict[str, str] = bidict()
    for market in exchange_info["markets"]:
        if market["type"] == "PERPETUAL":
            symbol = market["marketDisplayName"]
            pair = combine_to_hb_trading_pair(market["baseAsset"], market["quoteAsset"])
            mapping[symbol] = pair
    connector._set_trading_pair_symbol_map(mapping)
