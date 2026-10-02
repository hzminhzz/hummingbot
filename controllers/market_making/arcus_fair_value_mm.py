from decimal import Decimal
from typing import List, Optional

from pydantic import Field, model_validator

from hummingbot.core.data_type.common import PositionMode
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

    # Existing deterministic strategy policy inputs. These are intentionally kept
    # separate from Hummingbot's generic PMM spread fields because the strategy
    # computes quote prices from the tested fair-value policy.
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
    """Native controller shell.

    Issue #9 intentionally emits no executor actions. The tested strategy policy
    is wired behind this seam in #10 and production ExecutorActions in #11.
    """

    def __init__(self, config: ArcusFairValueMMConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        return []

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        return []
