from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .microstructure import FillSide, MicrostructureState
from .operating import OperatingState
from .risk import RiskState

_BPS = Decimal("10000")


class QuoteAction(str, Enum):
    PLACE = "PLACE"
    KEEP = "KEEP"
    MOVE = "MOVE"
    CANCEL = "CANCEL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class FairValueState:
    price: Decimal
    observed_at_ns: int
    stale_after_ns: int
    source: str
    confidence: Decimal


@dataclass(frozen=True)
class MarketState:
    symbol: str
    bid: Decimal
    ask: Decimal
    oracle: Decimal
    mark: Decimal
    observed_at_ns: int
    sequence_id: int
    min_order_size: Decimal = Decimal("0")
    min_notional_size: Decimal = Decimal("0")


@dataclass(frozen=True)
class RestingQuote:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class QuoteIntent:
    action: QuoteAction
    price: Decimal | None
    size: Decimal | None
    reason: str
    state_id: str


@dataclass(frozen=True)
class QuoteDecision:
    bid: QuoteIntent
    ask: QuoteIntent
    state: OperatingState = OperatingState.GOOD
    state_reason: str = "NORMAL"


@dataclass(frozen=True)
class QuotePolicyConfig:
    quote_notional: Decimal
    max_quote_deviation_bps: Decimal
    max_reference_disagreement_bps: Decimal

    max_abs_inventory: Decimal | None = None
    inventory_skew_at_limit: Decimal = Decimal("0")
    reprice_hysteresis_bps: Decimal = Decimal("0")
    market_loss_limit: Decimal | None = None
    account_loss_limit: Decimal | None = None
    risk_recovery_fraction: Decimal = Decimal("0.8")

    volatile_threshold_bps: Decimal | None = None
    volatile_recovery_bps: Decimal | None = None
    volatile_size_multiplier: Decimal = Decimal("1")
    toxic_markout_threshold_bps: Decimal | None = None
    toxic_recovery_bps: Decimal = Decimal("0")


class QuotePolicy:
    def __init__(self, config: QuotePolicyConfig):
        if config.quote_notional <= 0:
            raise ValueError("quote_notional must be positive")
        if config.max_abs_inventory is not None and config.max_abs_inventory <= 0:
            raise ValueError("max_abs_inventory must be positive")
        if not Decimal("0") <= config.inventory_skew_at_limit <= Decimal("1"):
            raise ValueError("inventory_skew_at_limit must be between 0 and 1")
        if config.reprice_hysteresis_bps < 0:
            raise ValueError("reprice_hysteresis_bps must be non-negative")
        if not Decimal("0") < config.risk_recovery_fraction < Decimal("1"):
            raise ValueError("risk_recovery_fraction must be between 0 and 1")
        if not Decimal("0") < config.volatile_size_multiplier <= Decimal("1"):
            raise ValueError("volatile_size_multiplier must be in (0, 1]")
        self.config = config

    def decide(
        self,
        market: MarketState,
        fair: FairValueState,
        now_ns: int,
        resting: dict[str, RestingQuote] | None = None,
        risk: RiskState | None = None,
        microstructure: MicrostructureState | None = None,
    ) -> QuoteDecision:
        resting = resting or {}
        state_id = f"{market.symbol}:{market.sequence_id}:{fair.observed_at_ns}"

        if fair.price <= 0 or fair.confidence <= 0:
            return self._fail_closed(resting, state_id, "INVALID_REFERENCE", OperatingState.FAULT)

        if now_ns - fair.observed_at_ns > fair.stale_after_ns:
            return self._fail_closed(resting, state_id, "STALE_REFERENCE", OperatingState.FAULT)

        if self._reference_diverged(market.oracle, fair.price) or self._reference_diverged(market.mark, fair.price):
            return self._fail_closed(resting, state_id, "REFERENCE_DIVERGENCE", OperatingState.FAULT)

        if risk is not None and not risk.data_healthy:
            return self._fail_closed(resting, state_id, "RISK_DATA_FAULT", OperatingState.FAULT)

        micro_state, micro_reason, micro_multiplier, toxic_bid, toxic_ask = self._microstructure_effects(
            microstructure
        )
        bid_inventory_multiplier, ask_inventory_multiplier = self._inventory_size_multipliers(risk)
        bid_inventory_limit = None
        ask_inventory_limit = None
        if risk is not None and self.config.max_abs_inventory is not None:
            bid_inventory_limit = max(
                Decimal("0"),
                self.config.max_abs_inventory - risk.position_base,
            )
            ask_inventory_limit = max(
                Decimal("0"),
                self.config.max_abs_inventory + risk.position_base,
            )

        upper_bid = fair.price * (Decimal("1") + self.config.max_quote_deviation_bps / _BPS)
        lower_ask = fair.price * (Decimal("1") - self.config.max_quote_deviation_bps / _BPS)

        bid = self._side_intent(
            side="bid",
            safe=market.bid <= upper_bid,
            resting_safe=resting.get("bid") is None or resting["bid"].price <= upper_bid,
            target_price=market.bid,
            state_id=state_id,
            resting=resting.get("bid"),
            unsafe_reason="BID_ABOVE_FAIR_VALUE_BOUND",
            size_multiplier=bid_inventory_multiplier * micro_multiplier,
            max_size=bid_inventory_limit,
            min_order_size=market.min_order_size,
            min_notional_size=market.min_notional_size,
        )
        ask = self._side_intent(
            side="ask",
            safe=market.ask >= lower_ask,
            resting_safe=resting.get("ask") is None or resting["ask"].price >= lower_ask,
            target_price=market.ask,
            state_id=state_id,
            resting=resting.get("ask"),
            unsafe_reason="ASK_BELOW_FAIR_VALUE_BOUND",
            size_multiplier=ask_inventory_multiplier * micro_multiplier,
            max_size=ask_inventory_limit,
            min_order_size=market.min_order_size,
            min_notional_size=market.min_notional_size,
        )

        if toxic_bid:
            bid = self._closed_side(resting.get("bid"), state_id, "BID_TOXIC")
        if toxic_ask:
            ask = self._closed_side(resting.get("ask"), state_id, "ASK_TOXIC")

        if risk is not None:
            pause_reason = self._risk_pause_reason(risk)
            if pause_reason is not None:
                return self._risk_paused_decision(
                    bid=bid,
                    ask=ask,
                    resting=resting,
                    state_id=state_id,
                    position=risk.position_base,
                    reason=pause_reason,
                )

        return QuoteDecision(
            bid=bid,
            ask=ask,
            state=micro_state,
            state_reason=micro_reason,
        )

    def _risk_pause_reason(self, risk: RiskState) -> str | None:
        if risk.previous_state is OperatingState.RISK_PAUSED:
            market_recovered = (
                self.config.market_loss_limit is None
                or risk.market_pnl > -(self.config.market_loss_limit * self.config.risk_recovery_fraction)
            )
            account_recovered = (
                self.config.account_loss_limit is None
                or risk.account_pnl > -(self.config.account_loss_limit * self.config.risk_recovery_fraction)
            )
            if not (market_recovered and account_recovered):
                return "RISK_RECOVERY_BUFFER"

        if self.config.market_loss_limit is not None and risk.market_pnl <= -self.config.market_loss_limit:
            return "MARKET_LOSS_LIMIT"
        if self.config.account_loss_limit is not None and risk.account_pnl <= -self.config.account_loss_limit:
            return "ACCOUNT_LOSS_LIMIT"
        return None

    def _risk_paused_decision(
        self,
        bid: QuoteIntent,
        ask: QuoteIntent,
        resting: dict[str, RestingQuote],
        state_id: str,
        position: Decimal,
        reason: str,
    ) -> QuoteDecision:
        if position > 0:
            bid = self._closed_side(resting.get("bid"), state_id, reason)
        elif position < 0:
            ask = self._closed_side(resting.get("ask"), state_id, reason)
        else:
            bid = self._closed_side(resting.get("bid"), state_id, reason)
            ask = self._closed_side(resting.get("ask"), state_id, reason)
        return QuoteDecision(
            bid=bid,
            ask=ask,
            state=OperatingState.RISK_PAUSED,
            state_reason=reason,
        )

    def _inventory_size_multipliers(self, risk: RiskState | None) -> tuple[Decimal, Decimal]:
        if risk is None or self.config.max_abs_inventory is None or self.config.inventory_skew_at_limit == 0:
            return Decimal("1"), Decimal("1")
        utilization = min(abs(risk.position_base) / self.config.max_abs_inventory, Decimal("1"))
        worsening_multiplier = max(
            Decimal("0"),
            Decimal("1") - utilization * self.config.inventory_skew_at_limit,
        )
        if risk.position_base > 0:
            return worsening_multiplier, Decimal("1")
        if risk.position_base < 0:
            return Decimal("1"), worsening_multiplier
        return Decimal("1"), Decimal("1")

    def _microstructure_effects(
        self,
        microstructure: MicrostructureState | None,
    ) -> tuple[OperatingState, str, Decimal, bool, bool]:
        if microstructure is None:
            return OperatingState.GOOD, "NORMAL", Decimal("1"), False, False

        toxic_bid = getattr(microstructure, "bid_toxic_override", False) or self._side_is_toxic(
            current=microstructure.bid_recent_markout_bps,
            side=FillSide.BID,
            microstructure=microstructure,
        )
        toxic_ask = getattr(microstructure, "ask_toxic_override", False) or self._side_is_toxic(
            current=microstructure.ask_recent_markout_bps,
            side=FillSide.ASK,
            microstructure=microstructure,
        )
        if toxic_bid or toxic_ask:
            if toxic_bid and toxic_ask:
                reason = "BOTH_SIDES_TOXIC"
            elif toxic_bid:
                reason = "BID_TOXIC"
            else:
                reason = "ASK_TOXIC"
            return OperatingState.TOXIC, reason, Decimal("1"), toxic_bid, toxic_ask

        if self._is_volatile(microstructure):
            return (
                OperatingState.VOLATILE,
                (
                    "VOLATILITY_ABOVE_RECOVERY"
                    if microstructure.previous_state is OperatingState.VOLATILE
                    else "VOLATILITY_HIGH"
                ),
                self.config.volatile_size_multiplier,
                False,
                False,
            )

        return OperatingState.GOOD, "NORMAL", Decimal("1"), False, False

    def _side_is_toxic(
        self,
        current: Decimal | None,
        side: FillSide,
        microstructure: MicrostructureState,
    ) -> bool:
        threshold = self.config.toxic_markout_threshold_bps
        if threshold is None:
            return False
        if current is not None and current <= threshold:
            return True
        if (
            microstructure.previous_state is OperatingState.TOXIC
            and microstructure.previous_toxic_side is side
        ):
            return current is None or current < self.config.toxic_recovery_bps
        return False

    def _is_volatile(self, microstructure: MicrostructureState) -> bool:
        threshold = self.config.volatile_threshold_bps
        if threshold is None:
            return False
        if microstructure.volatility_bps >= threshold:
            return True
        if microstructure.previous_state is OperatingState.VOLATILE:
            recovery = self.config.volatile_recovery_bps
            if recovery is None:
                recovery = threshold
            return microstructure.volatility_bps > recovery
        return False

    def _reference_diverged(self, reference: Decimal, fair: Decimal) -> bool:
        if reference <= 0:
            return True
        diff_bps = abs(reference - fair) / fair * _BPS
        return diff_bps > self.config.max_reference_disagreement_bps

    def _fail_closed(
        self,
        resting: dict[str, RestingQuote],
        state_id: str,
        reason: str,
        state: OperatingState = OperatingState.FAULT,
    ) -> QuoteDecision:
        return QuoteDecision(
            bid=self._closed_side(resting.get("bid"), state_id, reason),
            ask=self._closed_side(resting.get("ask"), state_id, reason),
            state=state,
            state_reason=reason,
        )

    @staticmethod
    def _closed_side(resting: RestingQuote | None, state_id: str, reason: str) -> QuoteIntent:
        if resting is not None:
            return QuoteIntent(QuoteAction.CANCEL, resting.price, resting.size, reason, state_id)
        return QuoteIntent(QuoteAction.HOLD, None, None, reason, state_id)

    def _side_intent(
        self,
        side: str,
        safe: bool,
        resting_safe: bool,
        target_price: Decimal,
        state_id: str,
        resting: RestingQuote | None,
        unsafe_reason: str,
        size_multiplier: Decimal = Decimal("1"),
        max_size: Decimal | None = None,
        min_order_size: Decimal = Decimal("0"),
        min_notional_size: Decimal = Decimal("0"),
    ) -> QuoteIntent:
        if not safe:
            if resting is not None:
                return QuoteIntent(QuoteAction.CANCEL, resting.price, resting.size, unsafe_reason, state_id)
            return QuoteIntent(QuoteAction.HOLD, None, None, unsafe_reason, state_id)

        target_size = self.config.quote_notional / target_price * size_multiplier
        if max_size is not None:
            target_size = min(target_size, max_size)
        if (
            target_size <= 0
            or target_size < min_order_size
            or target_size * target_price < min_notional_size
        ):
            return self._closed_side(resting, state_id, "INVENTORY_LIMIT_BELOW_MINIMUM")
        if resting is None:
            return QuoteIntent(
                QuoteAction.PLACE,
                target_price,
                target_size,
                f"{side.upper()}_SAFE_AT_TOUCH",
                state_id,
            )
        size_is_reduced = size_multiplier < Decimal("1") or (
            max_size is not None
            and target_size < self.config.quote_notional / target_price * size_multiplier
        )
        if size_is_reduced and resting.size > target_size:
            return QuoteIntent(
                QuoteAction.MOVE,
                target_price,
                target_size,
                f"{side.upper()}_RESIZE_AT_TOUCH",
                state_id,
            )
        if resting.price == target_price:
            return QuoteIntent(
                QuoteAction.KEEP,
                resting.price,
                resting.size,
                f"{side.upper()}_QUEUE_PRESERVED",
                state_id,
            )
        if resting_safe and self.config.reprice_hysteresis_bps > 0:
            drift_bps = abs(resting.price - target_price) / target_price * _BPS
            if drift_bps <= self.config.reprice_hysteresis_bps:
                return QuoteIntent(
                    QuoteAction.KEEP,
                    resting.price,
                    resting.size,
                    f"{side.upper()}_QUEUE_HYSTERESIS",
                    state_id,
                )
        return QuoteIntent(
            QuoteAction.MOVE,
            target_price,
            target_size,
            f"{side.upper()}_MOVE_TO_SAFE_TOUCH",
            state_id,
        )
