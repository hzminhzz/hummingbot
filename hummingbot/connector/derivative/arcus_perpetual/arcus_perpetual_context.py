import asyncio
from decimal import Decimal
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol

from bidict import bidict

from hummingbot.connector.client_order_tracker import ClientOrderTracker
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_auth import ArcusPerpetualAuth
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.in_flight_order import InFlightOrder
from hummingbot.core.data_type.trade_fee import TradeFeeSchema


class ArcusPerpetualContext(Protocol):
    _account_address: Optional[str]
    _account_index: int
    _account_balances: Dict[str, Decimal]
    _account_available_balances: Dict[str, Decimal]
    _api_signing_key: Optional[str]
    _arcus_auth: ArcusPerpetualAuth
    _market_info: Dict[str, Dict[str, Any]]
    _market_info_last_update: float
    _market_info_lock: asyncio.Lock
    _order_tracker: ClientOrderTracker
    _perpetual_trading: Any
    _time_synchronizer: TimeSynchronizer
    _trading_fees: Dict[str, TradeFeeSchema]
    _trading_pairs: List[str]
    _trading_rules: Dict[str, TradingRule]
    current_timestamp: float
    name: str

    def _time(self) -> float:
        ...

    def logger(self) -> Any:
        ...

    async def _api_get(
        self,
        path_url: str,
        params: Optional[Dict[str, Any]] = None,
        is_auth_required: bool = False,
        limit_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        ...

    async def _api_post(
        self,
        path_url: str,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, Any]] = None,
        is_auth_required: bool = False,
        limit_id: Optional[str] = None,
        return_err: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        ...

    async def exchange_symbol_associated_to_pair(self, trading_pair: str) -> str:
        ...

    async def trading_pair_associated_to_exchange_symbol(self, symbol: str) -> str:
        ...

    def _set_trading_pair_symbol_map(self, mapping: bidict[str, str]) -> None:
        ...

    def _account_query_params(self) -> Dict[str, Any]:
        ...

    async def _get_market_info(self, trading_pair: str) -> Dict[str, Any]:
        ...

    def _iter_user_event_queue(self) -> AsyncIterator[Dict[str, Any]]:
        ...

    def trade_fee_schema(self) -> TradeFeeSchema:
        ...

    async def _update_orders_fills(self, orders: List[InFlightOrder]) -> Any:
        ...
