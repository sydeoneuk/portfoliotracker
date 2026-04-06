from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class InstrumentEnrichment:
    ticker: str
    sector: Optional[str] = None
    industry: Optional[str] = None
    market_cap: Optional[float] = None
    description: Optional[str] = None
    country: Optional[str] = None


class BaseEnricher(ABC):
    @abstractmethod
    def enrich(self, ticker: str) -> Optional[InstrumentEnrichment]:
        """Fetch enrichment data for a single ticker. Return None if not found."""
        ...

    def enrich_batch(self, tickers: list[str]) -> dict[str, InstrumentEnrichment]:
        results = {}
        for ticker in tickers:
            data = self.enrich(ticker)
            if data:
                results[ticker] = data
        return results
