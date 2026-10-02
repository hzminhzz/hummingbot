import asyncio
from copy import copy
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_account as ACCOUNT
import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_market as MARKET
import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_order_state as ORDER_STATE_API
import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_orders as ORDERS
from hummingbot.connector.derivative.arcus_perpetual import (
    arcus_perpetual_constants as CONSTANTS,
    arcus_perpetual_web_utils as web_utils,
)
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_api_order_book_data_source import (
    ArcusPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_auth import ArcusPerpetualAuth
from hummingbot.connector.derivative.perpetual_budget_checker import PerpetualBudgetChecker
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate, PerpetualOrderCandidate
from hummingbot.core.data_type.perpetual_api_order_book_data_source import PerpetualAPIOrderBookDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


class ArcusPerpetualBudgetChecker(PerpetualBudgetChecker):
    """Budget checker for Arcus USD contracts collateralized in USDG."""

    def populate_collateral_entries(self, order_candidate: OrderCandidate) -> OrderCandidate:
        if not isinstance(order_candidate, PerpetualOrderCandidate):
            return super().populate_collateral_entries(order_candidate)

        candidate = copy(order_candidate)
        collateral_token = (
            self._exchange.get_buy_collateral_token(candidate.trading_pair)
            if candidate.order_side == TradeType.BUY
            else self._exchange.get_sell_collateral_token(candidate.trading_pair)
        )
        notional = candidate.amount * candidate.price

        if candidate.position_close:
            candidate.order_collateral = None
            candidate.potential_returns = TokenAmount(collateral_token, notional)
        else:
            candidate.order_collateral = TokenAmount(
                collateral_token,
                notional / candidate.leverage,
            )
            candidate.potential_returns = None

        fee = candidate._get_fee(self._exchange)
        candidate._populate_percent_fee_collateral_entry(self._exchange, fee)
        candidate._populate_fixed_fee_collateral_entries(fee)
        candidate._populate_percent_fee_value(self._exchange, fee)
        candidate._apply_fee_impact_on_potential_returns(self._exchange, fee)
        return candidate


class ArcusPerpetualDerivative(PerpetualDerivativePyBase):
    web_utils = web_utils
    SHORT_POLL_INTERVAL = 5.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    LONG_POLL_INTERVAL = 120.0

    def __init__(
        self,
        balance_asset_limit: Optional[Dict[str, Dict[str, Decimal]]] = None,
        rate_limits_share_pct: Decimal = Decimal("100"),
        api_signing_key: Optional[str] = None,
        account_address: Optional[str] = None,
        account_index: int = 0,
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        use_testnet: bool = False,
        domain: Optional[str] = None,
    ) -> None:
        self._api_signing_key = api_signing_key
        self._arcus_auth = ArcusPerpetualAuth(api_signing_key)
        self._account_address = account_address
        self._account_index = account_index
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs or []
        self._domain = domain or (
            CONSTANTS.TESTNET_DOMAIN if use_testnet else CONSTANTS.DEFAULT_DOMAIN
        )
        self._market_info: Dict[str, Dict[str, Any]] = {}
        self._market_info_last_update = 0.0
        self._market_info_lock = asyncio.Lock()
        super().__init__(balance_asset_limit, rate_limits_share_pct)
        self._budget_checker = ArcusPerpetualBudgetChecker(self)

    @property
    def name(self) -> str:
        return self._domain

    @property
    def authenticator(self) -> ArcusPerpetualAuth:
        return self._arcus_auth

    @property
    def rate_limits_rules(self) -> List[RateLimit]:
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def client_order_id_max_length(self) -> int:
        return 32

    @property
    def client_order_id_prefix(self) -> str:
        return "hb"

    @property
    def trading_rules_request_path(self) -> str:
        return CONSTANTS.MARKETS_PATH

    @property
    def trading_pairs_request_path(self) -> str:
        return CONSTANTS.MARKETS_PATH

    @property
    def check_network_request_path(self) -> str:
        return CONSTANTS.HEALTH_PATH

    @property
    def trading_pairs(self) -> List[str]:
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return False

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def funding_fee_poll_interval(self) -> int:
        return 60

    def supported_order_types(self) -> List[OrderType]:
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET]

    def supported_position_modes(self) -> List[PositionMode]:
        return [PositionMode.ONEWAY]

    def get_buy_collateral_token(self, trading_pair: str) -> str:
        return self._trading_rules[trading_pair].buy_order_collateral_token

    def get_sell_collateral_token(self, trading_pair: str) -> str:
        return self._trading_rules[trading_pair].sell_order_collateral_token

    _get_markets = MARKET.get_markets
    _get_market_info = MARKET.get_market_info
    _get_last_traded_price = MARKET.get_last_traded_price
    _update_trading_rules = MARKET.update_trading_rules
    _format_trading_rules = MARKET.format_trading_rules
    _initialize_trading_pair_symbols_from_exchange_info = MARKET.initialize_symbols

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(throttler=self._throttler, auth=self._auth)

    def _create_order_book_data_source(self) -> PerpetualAPIOrderBookDataSource:
        return ArcusPerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self._domain,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_user_stream_data_source import (
            ArcusPerpetualUserStreamDataSource,
        )

        return ArcusPerpetualUserStreamDataSource(
            auth=self._auth,
            account_address=self._account_address,
            account_index=self._account_index,
            api_factory=self._web_assistants_factory,
            domain=self._domain,
        )

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception) -> bool:
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return "HTTP status is 404" in str(status_update_exception)

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return "HTTP status is 404" in str(cancelation_exception)

    async def _trading_pair_position_mode_set(
        self, mode: PositionMode, trading_pair: str
    ) -> Tuple[bool, str]:
        supported = mode == PositionMode.ONEWAY
        return supported, "" if supported else "Arcus supports one-way positions only."

    _account_query_params = ACCOUNT.account_query_params
    _update_balances = ACCOUNT.update_balances
    _update_positions = ACCOUNT.update_positions

    async def _update_trading_fees(self):
        return

    _get_fee = ORDERS.get_fee

    _signed_request_headers = ORDERS.signed_request_headers
    _tick_size_for_price = staticmethod(ORDERS.tick_size_for_price)
    _quantize_price_to_tick = staticmethod(ORDERS.quantize_price_to_tick)
    _quantity_quantums = staticmethod(ORDERS.quantity_quantums)
    _good_til_time_us = ORDERS.good_til_time_us
    _place_order = ORDERS.place_order

    _place_cancel = ORDERS.place_cancel

    _request_order_status = ORDER_STATE_API.request_order_status

    _all_trade_updates_for_order = ORDER_STATE_API.all_trade_updates_for_order

    _set_trading_pair_leverage = ORDER_STATE_API.set_trading_pair_leverage

    _fetch_last_fee_payment = ORDER_STATE_API.fetch_last_fee_payment

    _position_from_market_data = ACCOUNT.position_from_market_data

    _process_account_attribute_entry = ACCOUNT.process_account_attribute_entry

    _process_order_update = ACCOUNT.process_order_update

    _process_user_fill = ACCOUNT.process_user_fill

    _user_stream_event_listener = ACCOUNT.listen_for_user_stream
