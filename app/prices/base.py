from dataclasses import dataclass
from abc import ABC, abstractmethod


@dataclass
class PriceResult:
    ticker: str          # T212 ticker (canonical key)
    price: float         # native currency
    price_gbp: float | None  # None when FX is unavailable; never assume parity
    currency: str        # ISO currency code of price


class PriceProvider(ABC):
    source: str

    @abstractmethod
    def fetch(self, instruments: list[dict]) -> list[PriceResult]:
        """
        instruments: list of dicts with keys:
            ticker, yf_ticker, currency, exchange
        Returns PriceResult for each ticker successfully priced.
        """
