import datetime
from app.valuation import effective_price_sql, get_fx_rates_to_gbp, value_holding
from app.snapshot_evidence import snapshot_quote_sql, snapshot_fields


def snapshot_csv_platforms(db) -> dict:
    try:
        return _snapshot_csv_platforms(db)
    except Exception:
        db.rollback()
        raise


def _snapshot_csv_platforms(db) -> dict:
    """Write today's HoldingSnapshot and PortfolioSnapshot rows for all non-API platforms.

    API platforms are snapshotted during their own SyncRunner run; this
    covers CSV-imported platforms (ii, T212 SIPP, etc.) so they also
    accumulate the daily history that drives the Value History chart.
    Requires instrument_prices to have been refreshed first.
    """
    from sqlalchemy import text as _t
    from app.models.holding_snapshot import HoldingSnapshot
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    today = datetime.date.today()
    now = datetime.datetime.utcnow()

    # Find all CSV-type platforms that have at least one position
    platforms = db.execute(_t("""
        SELECT DISTINCT pl.id, pl.user_id, pl.account_type
        FROM platforms pl
        WHERE pl.platform_type != 't212_api'
          AND pl.is_active = true
          AND (EXISTS (SELECT 1 FROM positions p WHERE p.platform_id = pl.id)
               OR EXISTS (SELECT 1 FROM portfolio_snapshots ps WHERE ps.platform_id = pl.id))
    """)).fetchall()

    written = 0
    for pl in platforms:
        # Fetch positions with best available price
        rows = db.execute(_t(f"""
            SELECT
                pos.ticker,
                pos.quantity,
                COALESCE(({snapshot_quote_sql('pos')})->>'normalized_currency', i.currency_code) AS currency,
                {snapshot_quote_sql('pos')} AS quote_evidence,
                {effective_price_sql('pos.ticker', 'pos.current_price')} AS price_native
            FROM positions pos
            JOIN instruments i ON i.ticker = pos.ticker
            WHERE pos.platform_id = :platform_id
              AND pos.user_id = :user_id
              AND pos.quantity > 0
        """), {"platform_id": pl.id, "user_id": pl.user_id}).fetchall()

        fx = get_fx_rates_to_gbp({row.currency for row in rows})
        # Validate the entire account before replacing any of today's data.
        values = {row.ticker: value_holding(row.quantity, row.price_native, row.currency, fx) for row in rows}
        account_total = 0.0
        db.execute(_t("""
            DELETE FROM holding_snapshots
            WHERE user_id = :uid AND platform_id = :pid AND snapshot_date = :day
        """), {"uid": pl.user_id, "pid": pl.id, "day": today})
        for row in rows:
            quantity = float(row.quantity or 0)
            price = float(values[row.ticker].price_native)
            value_gbp = float(values[row.ticker].value_gbp)
            account_total += value_gbp

            stmt = (
                pg_insert(HoldingSnapshot)
                .values(
                    user_id=pl.user_id,
                    snapshot_date=today,
                    captured_at=now,
                    platform_id=pl.id,
                    account=pl.account_type,
                    ticker=row.ticker,
                    quantity=quantity,
                    price_native=price,
                    value_gbp=value_gbp,
                    **snapshot_fields(row, values[row.ticker]),
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "snapshot_date", "platform_id", "ticker"],
                    set_={
                        "quantity": quantity,
                        "price_native": price,
                        "value_gbp": value_gbp,
                        "captured_at": now,
                        **snapshot_fields(row, values[row.ticker]),
                    },
                )
            )
            db.execute(stmt)
            written += 1

        # Write the account-level PortfolioSnapshot so the Value History chart
        # includes this platform alongside API-synced accounts.
        # Delete-then-insert mirrors SyncRunner; ON CONFLICT is unreliable when
        # pie_id is NULL because PostgreSQL treats NULLs as distinct.
        db.execute(_t("""
            DELETE FROM portfolio_snapshots
            WHERE user_id = :user_id
              AND platform_id = :platform_id
              AND snapshot_date = :snapshot_date
              AND scope_type = 'account'
        """), {"user_id": pl.user_id, "platform_id": pl.id, "snapshot_date": today})
        db.execute(_t("""
            INSERT INTO portfolio_snapshots
                (user_id, snapshot_date, captured_at, platform_id, account, scope_type, pie_id, total_value_gbp)
            VALUES
                (:user_id, :snapshot_date, :captured_at, :platform_id, :account, 'account', NULL, :total_value_gbp)
        """), {
            "user_id": pl.user_id,
            "snapshot_date": today,
            "captured_at": now,
            "platform_id": pl.id,
            "account": pl.account_type,
            "total_value_gbp": account_total,
        })

    db.commit()
    return {"platforms": len(platforms), "snapshots_written": written}


