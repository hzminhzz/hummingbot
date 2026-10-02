from enum import Enum
from sys import maxsize

from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

EXCHANGE_NAME = "arcus_perpetual"
DEFAULT_DOMAIN = EXCHANGE_NAME
TESTNET_DOMAIN = f"{EXCHANGE_NAME}_testnet"
USER_AGENT = "OpenAI File Downloader, XaiImageApiFetch/1.0"

REST_URLS = {
    DEFAULT_DOMAIN: "https://api.arcus.xyz",
    TESTNET_DOMAIN: "https://api.testnet.arcus.xyz",
}
WS_URLS = {
    DEFAULT_DOMAIN: "wss://api.arcus.xyz/v1/ws",
    TESTNET_DOMAIN: "wss://api.testnet.arcus.xyz/v1/ws",
}

HEALTH_PATH = "/health"
HEALTH_LIMIT_ID = HEALTH_PATH
TIME_PATH = "/v1/time"
MARKETS_PATH = "/v1/markets"
MARKETS_LIMIT_ID = MARKETS_PATH
BOOK_PATH = "/v1/l2OrderBook"
OPEN_ORDERS_PATH = "/v1/openOrders"
ORDER_PATH = "/v1/order"
ACCOUNT_PATH = "/v1/account"
FILLS_PATH = "/v1/fills"
POSITIONS_PATH = "/v1/positions"
FUNDING_RATES_PATH = "/v1/fundingRates"
FUNDING_PAYMENTS_PATH = "/v1/fundingPayments"
LEVERAGES_PATH = "/v1/leverages"
PLACE_ORDER_PATH = "/v1/placeOrder"
CANCEL_ORDER_PATH = "/v1/cancelOrder"
SET_LEVERAGE_PATH = "/v1/setLeverage"

WS_PUBLIC_CHANNELS = ("l2OrderbookUpdates", "trades", "oraclePrices", "markets", "funding")
WS_USER_CHANNELS = ("account", "positions", "userFills", "orders", "funding")

ORDER_STATE = {
    "PENDING": OrderState.PENDING_CREATE,
    "OPEN": OrderState.OPEN,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELED,
    "MARGIN_CANCELED": OrderState.CANCELED,
    "REJECTED": OrderState.FAILED,
    "EXPIRED": OrderState.CANCELED,
}

ORDERBOOK_LIMIT_ID = "l2OrderBook"
TIME_LIMIT_ID = "time"
IP_LIMIT_ID = "ip"
ACCOUNT_READ_LIMIT_ID = "account"
ORDER_STATUS_LIMIT_ID = "order"
OPEN_ORDERS_LIMIT_ID = "openOrders"
FILLS_LIMIT_ID = "fills"
POSITIONS_LIMIT_ID = "positions"
FUNDING_LIMIT_ID = "funding"
LEVERAGES_LIMIT_ID = "leverages"
SET_LEVERAGE_LIMIT_ID = "setLeverage"
PLACE_ORDER_LIMIT_ID = "placeOrder"
CANCEL_ORDER_LIMIT_ID = "cancelOrder"
NO_LIMIT = maxsize

# Arcus uses a 1,500-weight per-IP bucket refilling over 60 seconds. The
# subaccount order/cancel pools are enforced by the server and scale with volume.
RATE_LIMITS = [
    RateLimit(limit_id=IP_LIMIT_ID, limit=1500, time_interval=60),
    RateLimit(limit_id=HEALTH_LIMIT_ID, limit=NO_LIMIT, time_interval=60),
    RateLimit(
        limit_id=MARKETS_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=20)],
    ),
    RateLimit(
        limit_id=TIME_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=1)],
    ),
    RateLimit(
        limit_id=ORDERBOOK_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=7)],
    ),
    RateLimit(
        limit_id=ACCOUNT_READ_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=2)],
    ),
    RateLimit(
        limit_id=ORDER_STATUS_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=2)],
    ),
    RateLimit(
        limit_id=OPEN_ORDERS_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=20)],
    ),
    RateLimit(
        limit_id=FILLS_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=20)],
    ),
    RateLimit(
        limit_id=POSITIONS_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=2)],
    ),
    RateLimit(
        limit_id=FUNDING_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=20)],
    ),
    RateLimit(
        limit_id=LEVERAGES_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=2)],
    ),
    RateLimit(
        limit_id=SET_LEVERAGE_LIMIT_ID,
        limit=NO_LIMIT,
        time_interval=60,
        linked_limits=[LinkedLimitWeightPair(limit_id=IP_LIMIT_ID, weight=125)],
    ),
    RateLimit(limit_id=PLACE_ORDER_LIMIT_ID, limit=NO_LIMIT, time_interval=60),
    RateLimit(limit_id=CANCEL_ORDER_LIMIT_ID, limit=NO_LIMIT, time_interval=60),
]


class WSMessageType(str, Enum):
    SUBSCRIBED = "subscribed"
    CHANNEL_DATA = "channel_data"
    ERROR = "error"
