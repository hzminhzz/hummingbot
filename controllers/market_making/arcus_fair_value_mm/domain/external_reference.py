from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ExternalReferenceQuote:
    symbol: str
    price: Decimal
    observed_at_ns: int
    source: str


def parse_yahoo_chart(payload: dict) -> ExternalReferenceQuote:
    result = payload.get("chart", {}).get("result") or []
    if not result:
        raise RuntimeError("Yahoo chart response has no result")
    meta = result[0].get("meta", {})
    symbol = str(meta["symbol"])
    price = meta.get("regularMarketPrice")
    timestamp = meta.get("regularMarketTime")
    if price is None or timestamp is None:
        raise RuntimeError(f"Yahoo chart response missing price/time for {symbol}")
    return ExternalReferenceQuote(
        symbol=symbol,
        price=Decimal(str(price)),
        observed_at_ns=int(timestamp) * 1_000_000_000,
        source=f"yahoo:{symbol}",
    )


class YahooReferenceClient:
    USER_AGENT = "OpenAI File Downloader, XaiImageApiFetch/1.0"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def get(self, symbol: str) -> ExternalReferenceQuote:
        ticker = symbol.removesuffix("-USD")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
        request = Request(url, headers={"User-Agent": self.USER_AGENT})
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return parse_yahoo_chart(payload)
