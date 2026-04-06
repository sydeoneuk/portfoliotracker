"""
Entry point for syncing Trading 212 data and enriching dividends.

Usage:
    python scripts/sync.py                    # full T212 portfolio sync
    python scripts/sync.py --migrate          # run DB migrations first, then sync
    python scripts/sync.py --dividends        # sync T212 data + enrich all dividends
    python scripts/sync.py --dividends-only   # skip T212 sync, only refresh dividends
    python scripts/sync.py --dividends --tickers VWRP.L AAPL  # specific instruments
"""
import argparse
import logging
import subprocess
import sys

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

from app.config import settings
from app.database import SessionLocal
from app.sync.runner import SyncRunner
from app.sync.dividend_sync import DividendSync


def run_migrations():
    print("Running database migrations...")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    print("Migrations complete.")


def main():
    parser = argparse.ArgumentParser(description="Trading 212 data sync")
    parser.add_argument("--migrate", action="store_true", help="Run Alembic migrations first")
    parser.add_argument("--dividends", action="store_true", help="Also enrich dividend data")
    parser.add_argument(
        "--dividends-only",
        action="store_true",
        help="Skip T212 sync — only refresh dividend enrichment",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        metavar="TICKER",
        help="Limit dividend sync to these T212 tickers (e.g. VWRPl_EQ)",
    )
    parser.add_argument(
        "--lookback-years",
        type=int,
        default=5,
        help="Years of dividend history to fetch (default: 5)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch dividends even if recently synced",
    )
    args = parser.parse_args()

    if args.migrate:
        run_migrations()

    # Build account list — always sync the main (Trading) account;
    # add ISA if credentials are configured.
    accounts = [("Trading", settings.t212_api_key, settings.t212_api_secret)]
    if settings.t212_isa_api_key and settings.t212_isa_api_secret:
        accounts.append(("ISA", settings.t212_isa_api_key, settings.t212_isa_api_secret))

    session = SessionLocal()
    try:
        if not args.dividends_only:
            for account_label, api_key, api_secret in accounts:
                runner = SyncRunner(session, account=account_label,
                                    api_key=api_key, api_secret=api_secret)
                runner.sync_all()

        if args.dividends or args.dividends_only:
            div_sync = DividendSync(
                session=session,
                fmp_api_key=settings.fmp_api_key,
                lookback_years=args.lookback_years,
                force_refresh=args.force,
            )
            div_sync.sync_all(tickers=args.tickers)
    finally:
        session.close()


if __name__ == "__main__":
    main()
