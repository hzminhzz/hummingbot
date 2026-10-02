import asyncio
from decimal import Decimal
from typing import Callable, List, Optional

from arcus_mm.external_reference import YahooReferenceClient
from arcus_mm.microstructure import MicrostructureState
from arcus_mm.quote_policy import (
    FairValueState,
    MarketState,
    QuoteDecision,
    QuotePolicy,
    QuotePolicyConfig,
    RestingQuote,
)
from arcus_mm.risk import RiskState
from pydantic import Field, model_validator

from hummingbot.core.data_type.common import PositionMode, PriceType, TradeType
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction


class ArcusFairValueMMConfig(MarketMakingControllerConfigBase):
    """Standard Hummingbot V2 configuration for the Arcus fair-value market maker."""

    controller_name: str = "arcus_fair_value_mm"
    connector_name: str = "arcus_perpetual"
    trading_pair: str = "SPY-USD"
    leverage: int = 1
    position_mode: PositionMode = PositionMode.ONEWAY
    observation_only: bool = Field(default=True)

    quote_notional: Optional[Decimal] = Field(default=None, gt=0)
    max_quote_deviation_bps: Decimal = Field(default=Decimal("5"), ge=0)
    max_reference_disagreement_bps: Decimal = Field(default=Decimal("20"), ge=0)
    reference_stale_after_seconds: Decimal = Field(default=Decimal("5"), gt=0)
    external_reference_symbol: Optional[str] = None
    external_reference_source: str = "yahoo"

    max_abs_inventory: Optional[Decimal] = Field(default=None, gt=0)
    inventory_skew_at_limit: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    market_loss_limit: Optional[Decimal] = Field(default=None, gt=0)
    account_loss_limit: Optional[Decimal] = Field(default=None, gt=0)
    risk_recovery_fraction: Decimal = Field(default=Decimal("0.8"), gt=0, lt=1)

    volatile_threshold_bps: Optional[Decimal] = Field(default=None, ge=0)
    volatile_recovery_bps: Optional[Decimal] = Field(default=None, ge=0)
    volatile_size_multiplier: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    toxic_markout_threshold_bps: Optional[Decimal] = None
    toxic_recovery_bps: Decimal = Decimal("0")

    @model_validator(mode="after")
    def default_quote_notional_to_total_amount(self):
        if self.quote_notional is None:
            self.quote_notional = self.total_amount_quote
        return self


class ArcusFairValueMMController(MarketMakingControllerBase):
    """Native Hummingbot adapter around the tested arcus_mm strategy policy."""

    def __init__(
        self,
        config: ArcusFairValueMMConfig,
        *args,
        external_reference_client=None,
        microstructure_state_provider: Optional[Callable[[], Optional[MicrostructureState]]] = None,
        **kwargs,
    ):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._external_reference_client = external_reference_client or YahooReferenceClient()
        self._microstructure_state_provider = microstructure_state_provider or (lambda: None)
        self._policy = QuotePolicy(self._quote_policy_config())

    def _quote_policy_config(self) -> QuotePolicyConfig:
        return QuotePolicyConfig(
            quote_notional=self.config.quote_notional,
            max_quote_deviation_bps=self.config.max_quote_deviation_bps,
            max_reference_disagreement_bps=self.config.max_reference_disagreement_bps,
            max_abs_inventory=self.config.max_abs_inventory,
            inventory_skew_at_limit=self.config.inventory_skew_at_limit,
            market_loss_limit=self.config.market_loss_limit,
            account_loss_limit=self.config.account_loss_limit,
            risk_recovery_fraction=self.config.risk_recovery_fraction,
            volatile_threshold_bps=self.config.volatile_threshold_bps,
            volatile_recovery_bps=self.config.volatile_recovery_bps,
            volatile_size_multiplier=self.config.volatile_size_multiplier,
            toxic_markout_threshold_bps=self.config.toxic_markout_threshold_bps,
            toxic_recovery_bps=self.config.toxic_recovery_bps,
        )

    def _current_risk_state(self) -> RiskState:
        position_base = Decimal("0")
        market_pnl = Decimal("0")
        for position in self.positions_held:
            if (
                position.connector_name == self.config.connector_name
                and position.trading_pair == self.config.trading_pair
            ):
                amount = Decimal(position.amount)
                position_base += amount if position.side == TradeType.BUY else -amount
                market_pnl += Decimal(position.global_pnl_quote)

        account_pnl = (
            Decimal(self.performance_report.global_pnl_quote)
            if self.performance_report is not None
            else market_pnl
        )
        return RiskState(
            position_base=position_base,
            market_pnl=market_pnl,
            account_pnl=account_pnl,
        )

    def _current_resting_quotes(self) -> dict[str, RestingQuote]:
        resting = {}
        for executor in self.executors_info:
            if not executor.is_active:
                continue
            config = executor.config
            if getattr(config, "connector_name", None) != self.config.connector_name:
                continue
            if getattr(config, "trading_pair", None) != self.config.trading_pair:
                continue
            price = getattr(config, "price", None)
            amount = getattr(config, "amount", None)
            side = getattr(config, "side", None)
            if price is None or amount is None or side not in (TradeType.BUY, TradeType.SELL):
                continue
            resting["bid" if side == TradeType.BUY else "ask"] = RestingQuote(
                price=Decimal(price),
                size=Decimal(amount),
            )
        return resting

    async def update_processed_data(self):
        now_ns = int(self.market_data_provider.time() * 1_000_000_000)
        bid = Decimal(
            self.market_data_provider.get_price_by_type(
                self.config.connector_name,
                self.config.trading_pair,
                PriceType.BestBid,
            )
        )
        ask = Decimal(
            self.market_data_provider.get_price_by_type(
                self.config.connector_name,
                self.config.trading_pair,
                PriceType.BestAsk,
            )
        )
        funding_info = self.market_data_provider.get_funding_info(
            self.config.connector_name,
            self.config.trading_pair,
        )

        reference_symbol = self.config.external_reference_symbol or self.config.trading_pair
        external = await asyncio.to_thread(self._external_reference_client.get, reference_symbol)
        market = MarketState(
            symbol=self.config.trading_pair,
            bid=bid,
            ask=ask,
            oracle=Decimal(funding_info.index_price),
            mark=Decimal(funding_info.mark_price),
            observed_at_ns=now_ns,
            sequence_id=now_ns,
        )
        fair = FairValueState(
            price=Decimal(external.price),
            observed_at_ns=int(external.observed_at_ns),
            stale_after_ns=int(self.config.reference_stale_after_seconds * Decimal("1000000000")),
            source=external.source,
            confidence=Decimal("1"),
        )
        risk = self._current_risk_state()
        microstructure = self._microstructure_state_provider()
        decision: QuoteDecision = self._policy.decide(
            market=market,
            fair=fair,
            now_ns=now_ns,
            resting=self._current_resting_quotes(),
            risk=risk,
            microstructure=microstructure,
        )
        self.processed_data = {
            "market_state": market,
            "fair_value_state": fair,
            "risk_state": risk,
            "microstructure_state": microstructure,
            "quote_decision": decision,
            "state": decision.state,
            "state_reason": decision.state_reason,
            "reference_price": fair.price,
            "spread_multiplier": Decimal("1"),
        }

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        return []

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        return []
