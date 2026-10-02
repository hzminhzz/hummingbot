from dataclasses import dataclass
from decimal import Decimal

from .operating import OperatingState


@dataclass(frozen=True)
class RiskState:
    position_base: Decimal
    market_pnl: Decimal
    account_pnl: Decimal
    data_healthy: bool = True
    previous_state: OperatingState = OperatingState.GOOD
