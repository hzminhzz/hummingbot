from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .operating import OperatingState

_BPS = Decimal("10000")


class FillSide(str, Enum):
    BID = "BID"
    ASK = "ASK"


@dataclass(frozen=True)
class MarkoutSnapshot:
    one_s_bps: Decimal | None = None
    five_s_bps: Decimal | None = None
    thirty_s_bps: Decimal | None = None


@dataclass(frozen=True)
class MicrostructureState:
    volatility_bps: Decimal
    bid_recent_markout_bps: Decimal | None = None
    ask_recent_markout_bps: Decimal | None = None
    previous_state: OperatingState = OperatingState.GOOD
    previous_toxic_side: FillSide | None = None


def signed_markout_bps(side: FillSide, fill_price: Decimal, future_price: Decimal) -> Decimal:
    if fill_price <= 0:
        raise ValueError("fill_price must be positive")
    raw = (future_price - fill_price) / fill_price * _BPS
    return raw if side is FillSide.BID else -raw


def compute_markouts(
    side: FillSide,
    fill_price: Decimal,
    future_prices: dict[int, Decimal],
) -> MarkoutSnapshot:
    def at(seconds: int) -> Decimal | None:
        price = future_prices.get(seconds)
        return None if price is None else signed_markout_bps(side, fill_price, price)

    return MarkoutSnapshot(
        one_s_bps=at(1),
        five_s_bps=at(5),
        thirty_s_bps=at(30),
    )
