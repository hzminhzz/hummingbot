import asyncio
from decimal import Decimal
from itertools import islice
from typing import Callable, List, Optional

from pydantic import Field, model_validator

from hummingbot.core.data_type.common import PositionMode, PositionSide, PriceType, TradeType
from hummingbot.core.event.event_forwarder import EventForwarder
from hummingbot.core.event.events import OrderBookEvent, OrderBookTradeEvent
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction

from .domain.concordance import TradeFlowWindow, concordance_snapshot, depth_imbalance
from .domain.external_reference import ExternalReferenceQuote
from .domain.microstructure import MicrostructureState
from .domain.operating import OperatingState
from .domain.quote_policy import (
    FairValueState,
    MarketState,
    QuoteAction,
    QuoteDecision,
    QuoteIntent,
    QuotePolicy,
    QuotePolicyConfig,
    RestingQuote,
)
from .domain.risk import RiskState


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
    external_reference_symbol: str = "XYZ:SP500-USD"
    external_reference_source: str = "hyperliquid_sp500"
    external_reference_connector: str = "hyperliquid_perpetual"
    reference_anchor_target_price: Optional[Decimal] = Field(default=None, gt=0)
    reference_anchor_spy_price: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="Deprecated alias for reference_anchor_target_price.",
    )
    reference_anchor_hyperliquid_price: Optional[Decimal] = Field(default=None, gt=0)
    reference_anchor_timestamp_ns: Optional[int] = Field(default=None, gt=0)
    reference_anchor_max_age_seconds: Decimal = Field(default=Decimal("432000"), gt=0)
    reference_max_spread_bps: Decimal = Field(default=Decimal("20"), gt=0)

    max_abs_inventory: Optional[Decimal] = Field(default=None, gt=0)
    inventory_skew_at_limit: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    reprice_hysteresis_bps: Decimal = Field(default=Decimal("0"), ge=0)

    enable_concordance_veto: bool = Field(default=False)
    concordance_window_seconds: Decimal = Field(default=Decimal("5"), gt=0)
    concordance_book_levels: int = Field(default=5, ge=1, le=20)
    market_loss_limit: Optional[Decimal] = Field(default=None, gt=0)
    account_loss_limit: Optional[Decimal] = Field(default=None, gt=0)
    risk_recovery_fraction: Decimal = Field(default=Decimal("0.8"), gt=0, lt=1)

    volatile_threshold_bps: Optional[Decimal] = Field(default=None, ge=0)
    volatile_recovery_bps: Optional[Decimal] = Field(default=None, ge=0)
    volatile_size_multiplier: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    toxic_markout_threshold_bps: Optional[Decimal] = None
    toxic_recovery_bps: Decimal = Decimal("0")

    @model_validator(mode="after")
    def apply_compatibility_defaults(self):
        if self.quote_notional is None:
            self.quote_notional = self.total_amount_quote
        if self.reference_anchor_target_price is None and self.reference_anchor_spy_price is not None:
            self.reference_anchor_target_price = self.reference_anchor_spy_price
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
        self._external_reference_client = external_reference_client
        self._reference_order_book_initialized = False
        self._microstructure_state_provider = microstructure_state_provider
        self._concordance_trade_flow = TradeFlowWindow(self._observer_config_value("concordance_window_seconds"))
        self._concordance_trade_forwarder = EventForwarder(self._on_public_trade)
        self._concordance_order_book = None
        self._telemetry_cycles = 0
        self._telemetry_bid_eligible_cycles = 0
        self._telemetry_ask_eligible_cycles = 0
        self._telemetry_bid_toxic_cycles = 0
        self._telemetry_ask_toxic_cycles = 0
        self._telemetry_fault_cycles = 0
        self._policy = QuotePolicy(self._quote_policy_config())

    def _observer_config_value(self, name: str):
        return getattr(self.config, name)

    def _quote_policy_config(self) -> QuotePolicyConfig:
        return QuotePolicyConfig(
            quote_notional=self.config.quote_notional,
            max_quote_deviation_bps=self.config.max_quote_deviation_bps,
            max_reference_disagreement_bps=self.config.max_reference_disagreement_bps,
            max_abs_inventory=self.config.max_abs_inventory,
            inventory_skew_at_limit=self.config.inventory_skew_at_limit,
            reprice_hysteresis_bps=self.config.reprice_hysteresis_bps,
            market_loss_limit=self.config.market_loss_limit,
            account_loss_limit=self.config.account_loss_limit,
            risk_recovery_fraction=self.config.risk_recovery_fraction,
            volatile_threshold_bps=self.config.volatile_threshold_bps,
            volatile_recovery_bps=self.config.volatile_recovery_bps,
            volatile_size_multiplier=self.config.volatile_size_multiplier,
            toxic_markout_threshold_bps=self.config.toxic_markout_threshold_bps,
            toxic_recovery_bps=self.config.toxic_recovery_bps,
        )

    def _on_public_trade(self, event: OrderBookTradeEvent) -> None:
        if event.trading_pair != self.config.trading_pair:
            return
        self._concordance_trade_flow.add(
            timestamp=Decimal(str(event.timestamp)),
            is_buy=event.type is TradeType.BUY,
            amount=Decimal(str(event.amount)),
        )

    def _ensure_concordance_subscription(self):
        if not self.config.enable_concordance_veto or self._microstructure_state_provider is not None:
            return None
        order_book = self.market_data_provider.get_order_book(
            self.config.connector_name,
            self.config.trading_pair,
        )
        if order_book is self._concordance_order_book:
            return order_book
        if self._concordance_order_book is not None:
            try:
                self._concordance_order_book.remove_listener(
                    OrderBookEvent.TradeEvent,
                    self._concordance_trade_forwarder,
                )
            except Exception:
                self.logger().warning("Failed to detach prior Arcus trade listener.", exc_info=True)
        order_book.add_listener(OrderBookEvent.TradeEvent, self._concordance_trade_forwarder)
        self._concordance_order_book = order_book
        return order_book

    def _live_concordance_microstructure(self, now_ns: int) -> Optional[MicrostructureState]:
        order_book = self._ensure_concordance_subscription()
        if order_book is None:
            return None

        levels = self.config.concordance_book_levels
        bid_amounts = [Decimal(str(row.amount)) for row in islice(order_book.bid_entries(), levels)]
        ask_amounts = [Decimal(str(row.amount)) for row in islice(order_book.ask_entries(), levels)]
        if not bid_amounts or not ask_amounts:
            raise RuntimeError("Arcus order book has no usable depth for concordance veto")

        book_imbalance = depth_imbalance(bid_amounts, ask_amounts)
        trade_imbalance, trade_events = self._concordance_trade_flow.imbalance(
            Decimal(now_ns) / Decimal("1000000000")
        )
        snapshot = concordance_snapshot(
            book_imbalance=book_imbalance,
            trade_imbalance=trade_imbalance,
            trade_events=trade_events,
        )
        return MicrostructureState(
            volatility_bps=Decimal("0"),
            bid_toxic_override=snapshot.toxic_bid,
            ask_toxic_override=snapshot.toxic_ask,
            book_imbalance=snapshot.book_imbalance,
            trade_imbalance=snapshot.trade_imbalance,
        )

    def _current_microstructure_state(self, now_ns: int) -> Optional[MicrostructureState]:
        if self._microstructure_state_provider is not None:
            return self._microstructure_state_provider()
        if self.config.enable_concordance_veto:
            return self._live_concordance_microstructure(now_ns)
        return None

    def on_stop(self):
        if self._concordance_order_book is not None:
            try:
                self._concordance_order_book.remove_listener(
                    OrderBookEvent.TradeEvent,
                    self._concordance_trade_forwarder,
                )
            finally:
                self._concordance_order_book = None

    def _current_risk_state(self) -> RiskState:
        position_base = Decimal("0")
        market_pnl = Decimal("0")
        connector_positions = None
        try:
            connector = self.market_data_provider.get_connector(self.config.connector_name)
            connector_positions = connector.account_positions
        except (AttributeError, ValueError):
            connector_positions = None

        if connector_positions is not None:
            for position in connector_positions.values():
                if position.trading_pair != self.config.trading_pair:
                    continue
                amount = Decimal(position.amount)
                position_base += amount if position.position_side == PositionSide.LONG else -amount
                market_pnl += Decimal(position.unrealized_pnl)
        else:
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

    def _active_quote_executors(self) -> dict[str, object]:
        active = {}
        for executor in self.executors_info:
            if not executor.is_active:
                continue
            config = executor.config
            if getattr(config, "connector_name", None) != self.config.connector_name:
                continue
            if getattr(config, "trading_pair", None) != self.config.trading_pair:
                continue
            level_id = getattr(config, "level_id", None)
            if level_id == "arcus_bid":
                active["bid"] = executor
            elif level_id == "arcus_ask":
                active["ask"] = executor
        return active

    def _current_resting_quotes(self) -> dict[str, RestingQuote]:
        resting = {}
        for side, executor in self._active_quote_executors().items():
            config = executor.config
            price = getattr(config, "price", None)
            amount = getattr(config, "amount", None)
            if price is None or amount is None:
                continue
            resting[side] = RestingQuote(price=Decimal(price), size=Decimal(amount))
        return resting

    def _invalid_reference(self, source: str) -> ExternalReferenceQuote:
        return ExternalReferenceQuote(
            symbol=self.config.external_reference_symbol,
            price=Decimal("0"),
            observed_at_ns=0,
            source=source,
        )

    async def _hyperliquid_reference_quote(self, now_ns: int) -> ExternalReferenceQuote:
        if self._external_reference_client is not None:
            return await asyncio.to_thread(
                self._external_reference_client.get,
                self.config.external_reference_symbol,
            )

        anchor_target = self.config.reference_anchor_target_price
        anchor_hl = self.config.reference_anchor_hyperliquid_price
        anchor_ts = self.config.reference_anchor_timestamp_ns
        if anchor_target is None or anchor_hl is None or anchor_ts is None:
            return self._invalid_reference(f"{self.config.external_reference_source}:MISSING_ANCHOR")

        anchor_max_age_ns = int(self.config.reference_anchor_max_age_seconds * Decimal("1000000000"))
        if now_ns - anchor_ts > anchor_max_age_ns:
            return self._invalid_reference(f"{self.config.external_reference_source}:STALE_ANCHOR")

        if not self._reference_order_book_initialized:
            initialized = await self.market_data_provider.initialize_order_book(
                self.config.external_reference_connector,
                self.config.external_reference_symbol,
            )
            if not initialized:
                return self._invalid_reference(
                    f"{self.config.external_reference_source}:ORDER_BOOK_INIT_FAILED"
                )
            self._reference_order_book_initialized = True

        reference_connector = self.market_data_provider.get_connector_with_fallback(
            self.config.external_reference_connector
        )
        tracker = getattr(reference_connector, "order_book_tracker", None)
        data_source = getattr(tracker, "data_source", None)
        ws_assistant = getattr(data_source, "_ws_assistant", None)
        last_recv_time = float(getattr(ws_assistant, "last_recv_time", 0) or 0)
        now_seconds = now_ns / 1_000_000_000
        if (
            last_recv_time <= 0
            or now_seconds - last_recv_time > float(self.config.reference_stale_after_seconds)
        ):
            return self._invalid_reference(f"{self.config.external_reference_source}:STALE_PUBLIC_FEED")

        order_book = self.market_data_provider.get_order_book(
            self.config.external_reference_connector,
            self.config.external_reference_symbol,
        )
        bid = Decimal(str(order_book.get_price(False)))
        ask = Decimal(str(order_book.get_price(True)))
        if bid <= 0 or ask <= 0 or ask < bid:
            return self._invalid_reference(f"{self.config.external_reference_source}:INVALID_BOOK")

        mid = (bid + ask) / Decimal("2")
        spread_bps = (ask - bid) / mid * Decimal("10000")
        if spread_bps > self.config.reference_max_spread_bps:
            return self._invalid_reference(f"{self.config.external_reference_source}:WIDE_BOOK")

        ratio = anchor_target / anchor_hl
        return ExternalReferenceQuote(
            symbol=self.config.external_reference_symbol,
            price=mid * ratio,
            # Freshness is the local receive time of the public Hyperliquid WebSocket,
            # so an unchanged but healthy book remains valid while a dead socket fails closed.
            observed_at_ns=int(last_recv_time * 1_000_000_000),
            source=f"hyperliquid:{self.config.external_reference_symbol}:anchored_ratio",
        )

    def _fault_decision(self, reason: str, now_ns: int) -> QuoteDecision:
        resting = self._current_resting_quotes()
        state_id = f"{self.config.trading_pair}:fault:{now_ns}"

        def closed(side: str) -> QuoteIntent:
            quote = resting.get(side)
            if quote is None:
                return QuoteIntent(
                    action=QuoteAction.HOLD,
                    price=None,
                    size=None,
                    reason=reason,
                    state_id=state_id,
                )
            return QuoteIntent(
                action=QuoteAction.CANCEL,
                price=quote.price,
                size=quote.size,
                reason=reason,
                state_id=state_id,
            )

        return QuoteDecision(
            bid=closed("bid"),
            ask=closed("ask"),
            state=OperatingState.FAULT,
            state_reason=reason,
        )

    async def _update_processed_data_inner(self, now_ns: int):
        if getattr(self.market_data_provider, "ready", True) is False:
            raise RuntimeError("market data provider is not ready")

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
        if bid <= 0 or ask <= 0 or ask < bid:
            raise RuntimeError("invalid Arcus BBO")

        funding_info = self.market_data_provider.get_funding_info(
            self.config.connector_name,
            self.config.trading_pair,
        )

        external = await self._hyperliquid_reference_quote(now_ns)
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
        microstructure = self._current_microstructure_state(now_ns)
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
        self._record_telemetry(decision)

    async def update_processed_data(self):
        now_ns = int(self.market_data_provider.time() * 1_000_000_000)
        try:
            await self._update_processed_data_inner(now_ns)
        except Exception as exc:
            self.logger().error(
                "Arcus MVP data/policy update failed closed for %s: %s",
                self.config.trading_pair,
                exc,
                exc_info=True,
            )
            decision = self._fault_decision("MARKET_DATA_FAULT", now_ns)
            self.processed_data = {
                "market_state": None,
                "fair_value_state": None,
                "risk_state": None,
                "microstructure_state": None,
                "quote_decision": decision,
                "state": decision.state,
                "state_reason": decision.state_reason,
                "reference_price": Decimal("0"),
                "spread_multiplier": Decimal("1"),
            }
            self._telemetry_cycles += 1
            self._telemetry_fault_cycles += 1

    def _record_telemetry(self, decision: QuoteDecision) -> None:
        self._telemetry_cycles += 1
        if decision.bid.action in (QuoteAction.PLACE, QuoteAction.KEEP):
            self._telemetry_bid_eligible_cycles += 1
        if decision.ask.action in (QuoteAction.PLACE, QuoteAction.KEEP):
            self._telemetry_ask_eligible_cycles += 1
        if decision.bid.reason == "BID_TOXIC":
            self._telemetry_bid_toxic_cycles += 1
        if decision.ask.reason == "ASK_TOXIC":
            self._telemetry_ask_toxic_cycles += 1

    def get_custom_info(self) -> dict:
        decision = self.processed_data.get("quote_decision")
        fair = self.processed_data.get("fair_value_state")
        risk = self.processed_data.get("risk_state")
        micro = self.processed_data.get("microstructure_state")
        cycles = max(self._telemetry_cycles, 1)
        active = sorted(self._active_quote_executors().keys())

        def dec(value):
            return None if value is None else str(value)

        return {
            "pair": self.config.trading_pair,
            "observation_only": self.config.observation_only,
            "state": None if decision is None else decision.state.value,
            "state_reason": None if decision is None else decision.state_reason,
            "bid_action": None if decision is None else decision.bid.action.value,
            "bid_reason": None if decision is None else decision.bid.reason,
            "ask_action": None if decision is None else decision.ask.action.value,
            "ask_reason": None if decision is None else decision.ask.reason,
            "active_quote_sides": active,
            "position_base": dec(None if risk is None else risk.position_base),
            "reference_price": dec(None if fair is None else fair.price),
            "reference_source": None if fair is None else fair.source,
            "book_imbalance": dec(None if micro is None else micro.book_imbalance),
            "trade_imbalance": dec(None if micro is None else micro.trade_imbalance),
            "cycles": self._telemetry_cycles,
            "bid_eligible_ratio": self._telemetry_bid_eligible_cycles / cycles,
            "ask_eligible_ratio": self._telemetry_ask_eligible_cycles / cycles,
            "bid_toxic_ratio": self._telemetry_bid_toxic_cycles / cycles,
            "ask_toxic_ratio": self._telemetry_ask_toxic_cycles / cycles,
            "fault_ratio": self._telemetry_fault_cycles / cycles,
            "quote_notional": str(self.config.quote_notional),
            "max_abs_inventory": dec(self.config.max_abs_inventory),
            "rwa_market": self.config.trading_pair in {"SPY-USD", "QQQ-USD", "GLD-USD"},
        }

    def _create_quote_action(self, side: str, price: Decimal, amount: Decimal) -> CreateExecutorAction:
        trade_type = TradeType.BUY if side == "bid" else TradeType.SELL
        executor_config = OrderExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=trade_type,
            amount=amount,
            price=price,
            execution_strategy=ExecutionStrategy.LIMIT_MAKER,
            leverage=self.config.leverage,
            level_id=f"arcus_{side}",
        )
        return CreateExecutorAction(controller_id=self.config.id, executor_config=executor_config)

    def determine_executor_actions(self) -> List[ExecutorAction]:
        if self.config.observation_only:
            return []
        decision = self.processed_data.get("quote_decision")
        if decision is None:
            return []

        active = self._active_quote_executors()
        actions: List[ExecutorAction] = []
        for side, intent in (("bid", decision.bid), ("ask", decision.ask)):
            executor = active.get(side)
            if intent.action in (QuoteAction.CANCEL, QuoteAction.MOVE):
                if executor is not None:
                    actions.append(
                        StopExecutorAction(
                            controller_id=self.config.id,
                            executor_id=executor.id,
                            keep_position=True,
                        )
                    )
                continue
            if intent.action is QuoteAction.PLACE and executor is None:
                if intent.price is not None and intent.size is not None:
                    actions.append(self._create_quote_action(side, intent.price, intent.size))
        return actions

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        return [a for a in self.determine_executor_actions() if isinstance(a, CreateExecutorAction)]

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        return [a for a in self.determine_executor_actions() if isinstance(a, StopExecutorAction)]
