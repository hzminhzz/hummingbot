from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Deque, Iterable, Tuple

_ZERO = Decimal("0")


@dataclass(frozen=True)
class ConcordanceSnapshot:
    book_imbalance: Decimal
    trade_imbalance: Decimal
    toxic_bid: bool
    toxic_ask: bool
    trade_events: int


def depth_imbalance(
    bid_amounts: Iterable[Decimal],
    ask_amounts: Iterable[Decimal],
) -> Decimal:
    bid_depth = sum((Decimal(str(x)) for x in bid_amounts), _ZERO)
    ask_depth = sum((Decimal(str(x)) for x in ask_amounts), _ZERO)
    total = bid_depth + ask_depth
    if total <= 0:
        return _ZERO
    return (bid_depth - ask_depth) / total


class TradeFlowWindow:
    """Causal rolling aggressive-trade imbalance for one market."""

    def __init__(self, window_seconds: Decimal = Decimal("5")):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = Decimal(window_seconds)
        self._events: Deque[Tuple[Decimal, Decimal, Decimal]] = deque()

    def add(self, timestamp: Decimal, is_buy: bool, amount: Decimal) -> None:
        amount = Decimal(amount)
        if amount <= 0:
            return
        buy = amount if is_buy else _ZERO
        sell = amount if not is_buy else _ZERO
        self._events.append((Decimal(timestamp), buy, sell))
        self._prune(Decimal(timestamp))

    def _prune(self, now: Decimal) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def imbalance(self, now: Decimal) -> tuple[Decimal, int]:
        now = Decimal(now)
        self._prune(now)
        buy = sum((x[1] for x in self._events), _ZERO)
        sell = sum((x[2] for x in self._events), _ZERO)
        total = buy + sell
        imbalance = _ZERO if total <= 0 else (buy - sell) / total
        return imbalance, len(self._events)


def concordance_snapshot(
    book_imbalance: Decimal,
    trade_imbalance: Decimal,
    trade_events: int,
) -> ConcordanceSnapshot:
    book_imbalance = Decimal(book_imbalance)
    trade_imbalance = Decimal(trade_imbalance)
    toxic_bid = book_imbalance < 0 and trade_imbalance < 0
    toxic_ask = book_imbalance > 0 and trade_imbalance > 0
    return ConcordanceSnapshot(
        book_imbalance=book_imbalance,
        trade_imbalance=trade_imbalance,
        toxic_bid=toxic_bid,
        toxic_ask=toxic_ask,
        trade_events=trade_events,
    )
