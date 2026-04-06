"""
Fetch trailing 3-year average Free Cash Flow per share from yfinance
and store it on the instruments table.

Usage:
    python scripts/enrich_fcf.py                  # all held instruments
    python scripts/enrich_fcf.py --tickers AAPL MSFT  # specific yf tickers
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

import yfinance as yf
from sqlalchemy import text
from app.database import SessionLocal


def fetch_fcf_per_share(yf_ticker: str) -> float | None:
    """Return trailing 3-year average FCF per share, or None if unavailable."""
    try:
        t = yf.Ticker(yf_ticker)
        cf = t.cashflow  # annual, rows = metrics, cols = dates

        if cf is None or cf.empty:
            log.warning("%s: no cashflow data", yf_ticker)
            return None

        # Prefer pre-computed Free Cash Flow row; fall back to computing it
        if "Free Cash Flow" in cf.index:
            fcf_series = cf.loc["Free Cash Flow"]
        elif "Operating Cash Flow" in cf.index and "Capital Expenditure" in cf.index:
            fcf_series = cf.loc["Operating Cash Flow"] + cf.loc["Capital Expenditure"]
        else:
            log.warning("%s: missing FCF rows in cashflow", yf_ticker)
            return None

        fcf_series = fcf_series.dropna().sort_index(ascending=False)
        if fcf_series.empty:
            return None

        # Shares outstanding
        shares = None
        try:
            shares = t.fast_info.get("shares")
        except Exception:
            pass
        if not shares:
            shares = (t.info or {}).get("sharesOutstanding")
        if not shares or shares == 0:
            log.warning("%s: cannot determine shares outstanding", yf_ticker)
            return None

        fcf_ps = fcf_series / float(shares)
        avg = float(fcf_ps.head(3).mean())
        return avg

    except Exception as exc:
        log.error("%s: %s", yf_ticker, exc)
        return None


def fetch_info(yf_ticker: str) -> dict:
    """Return dict with eps_ttm, sector, industry from yfinance info."""
    try:
        info = yf.Ticker(yf_ticker).info or {}
        result = {}
        eps = info.get("trailingEps")
        if eps is not None:
            result["eps_ttm"] = float(eps)
        if info.get("sector"):
            result["sector"] = info["sector"]
        if info.get("industry"):
            result["industry"] = info["industry"]
        return result
    except Exception as exc:
        log.error("%s info: %s", yf_ticker, exc)
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", metavar="YF_TICKER",
                        help="Limit to these yfinance tickers (e.g. BP.L AAPL)")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        # Get instruments that have live positions and a yf_ticker
        query = """
            SELECT DISTINCT i.ticker, i.yf_ticker, i.name
            FROM instruments i
            JOIN positions p ON p.ticker = i.ticker
            WHERE i.yf_ticker IS NOT NULL
        """
        rows = session.execute(text(query)).fetchall()

        if args.tickers:
            rows = [r for r in rows if r.yf_ticker in args.tickers]

        log.info("Fetching FCF for %d instruments", len(rows))
        updated = 0

        for row in rows:
            log.info("  %s (%s)", row.name or row.ticker, row.yf_ticker)
            fcf_ps = fetch_fcf_per_share(row.yf_ticker)
            extra = fetch_info(row.yf_ticker)
            eps = extra.get("eps_ttm")
            sector = extra.get("sector")
            industry = extra.get("industry")
            if fcf_ps is not None or extra:
                session.execute(
                    text("""UPDATE instruments
                            SET fcf_per_share_3y_avg = COALESCE(:fcf, fcf_per_share_3y_avg),
                                eps_ttm     = COALESCE(:eps,     eps_ttm),
                                sector      = COALESCE(:sector,  sector),
                                industry    = COALESCE(:industry, industry)
                            WHERE ticker = :t"""),
                    {"fcf": fcf_ps, "eps": eps, "sector": sector,
                     "industry": industry, "t": row.ticker},
                )
                log.info("    → FCF/share 3y avg: %s  EPS: %s  sector: %s  industry: %s",
                         f"{fcf_ps:.4f}" if fcf_ps is not None else "n/a",
                         f"{eps:.4f}" if eps is not None else "n/a",
                         sector or "n/a", industry or "n/a")
                updated += 1
            else:
                log.info("    → no data")

        session.commit()
        log.info("Done — updated %d / %d instruments", updated, len(rows))
    finally:
        session.close()


if __name__ == "__main__":
    main()
