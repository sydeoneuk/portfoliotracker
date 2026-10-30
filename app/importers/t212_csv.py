"""
T212 CSV transaction importer.

Parses T212 account statement exports (ISA, Invest, SIPP) and upserts
records into the shared models: Instrument, Order, DividendPayment,
Transaction. After all rows are processed, positions are recomputed from
the full order history for the platform.

Expected CSV columns (T212 standard export):
  Action, Time (UTC), ISIN, Ticker, Name, Notes, ID,
  No. of shares, Price / share, Currency (Price / share),
  Exchange rate, Result, Total, Withholding tax,
  Currency (Withholding tax), Stamp duty reserve tax
"""

import csv
import datetime
import io
from dataclasses import dataclass, field
from typing import IO

from sqlalchemy import text as _text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    DividendPayment,
    Instrument,
    Order,
    Platform,
    Position,
    Transaction,
)

# ── Action classification ──────────────────────────────────────────────────

_BUY_ACTIONS = frozenset({
    "market buy", "limit buy", "stop buy", "buy",
})
_SELL_ACTIONS = frozenset({
    "market sell", "limit sell", "stop sell", "sell",
})
_DIVIDEND_ACTIONS = frozenset({
    "dividend (ordinary)",
    "dividend (dividend)",          # T212's label for ordinary dividends
    "dividend (manufactured payment)",
    "dividend (equalisation)",
    "dividend (property income distribution)",
    "dividend (return of capital)",
    "dividend",
})


def _order_type(action: str) -> str:
    a = action.lower()
    if a.startswith("limit"):
        return "LIMIT"
    if a.startswith("stop"):
        return "STOP"
    return "MARKET"


# ── Column normalisation ───────────────────────────────────────────────────

def _normalise_headers(headers: list[str]) -> dict[str, str]:
    """Return a {normalised_key: original_header} map for flexible column lookup."""
    result = {}
    for h in headers:
        key = h.strip().lower()
        # strip parenthetical currency suffixes: "price / share (gbp)" → "price / share"
        if key.endswith(")") and "(" in key:
            key = key[: key.rfind("(")].strip()
        result[key] = h
    return result


def _get(row: dict, headers_map: dict[str, str], *keys: str) -> str:
    """Fetch a cell by trying normalised key variants; return '' if not found."""
    for k in keys:
        orig = headers_map.get(k.lower().strip())
        if orig and orig in row:
            val = row[orig]
            if val is not None:
                return str(val).strip()
    return ""


def _float(s: str) -> float | None:
    try:
        return float(s.replace(",", "").replace("–", "-").replace("−", "-")) if s else None
    except ValueError:
        return None


def _parse_dt(s: str) -> datetime.datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ── Result dataclass ───────────────────────────────────────────────────────

@dataclass
class ImportResult:
    orders: int = 0
    dividends: int = 0
    cash_transactions: int = 0
    instruments_created: int = 0
    positions_upserted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.orders:
            parts.append(f"{self.orders} orders")
        if self.dividends:
            parts.append(f"{self.dividends} dividends")
        if self.cash_transactions:
            parts.append(f"{self.cash_transactions} cash transactions")
        if self.instruments_created:
            parts.append(f"{self.instruments_created} new instruments")
        if self.positions_upserted:
            parts.append(f"{self.positions_upserted} positions computed")
        if self.skipped:
            parts.append(f"{self.skipped} rows skipped")
        return "Imported: " + (", ".join(parts) or "nothing") + "."


# ── Importer ───────────────────────────────────────────────────────────────

class T212CsvImporter:
    def __init__(self, session: Session, platform: Platform, user_id: int):
        self.session = session
        self.platform = platform
        self.platform_id = platform.id
        self.user_id = user_id
        self._result = ImportResult()
        self._known_tickers: set[str] = set()

    # ── Public entry point ─────────────────────────────────────────────────

    def import_file(self, file_obj: IO[bytes] | IO[str]) -> ImportResult:
        raw = file_obj.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")  # strip BOM if present

        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            self._result.errors.append("CSV has no headers — is the file empty?")
            return self._result

        headers_map = _normalise_headers(list(reader.fieldnames))

        for i, row in enumerate(reader, start=2):
            try:
                self._process_row(row, headers_map)
            except Exception as exc:
                self._result.errors.append(f"Row {i}: {exc}")
                self.session.rollback()

        self.session.flush()
        self._rebuild_positions()
        self.session.commit()
        return self._result

    # ── Row dispatch ───────────────────────────────────────────────────────

    def _process_row(self, row: dict, hm: dict[str, str]) -> None:
        action = _get(row, hm, "action").lower().strip()
        if not action:
            self._result.skipped += 1
            return

        if action in _BUY_ACTIONS or action in _SELL_ACTIONS:
            self._import_order(row, hm, action)
        elif action in _DIVIDEND_ACTIONS:
            self._import_dividend(row, hm, action)
        else:
            self._import_transaction(row, hm, action)

    # ── Instrument helper ──────────────────────────────────────────────────

    def _ensure_instrument(
        self, isin: str, ticker: str, name: str, currency: str
    ) -> str | None:
        """Upsert a minimal instrument row; return ticker or None."""
        if not ticker:
            return None
        if ticker in self._known_tickers:
            return ticker
        existing = self.session.get(Instrument, ticker)
        now = datetime.datetime.utcnow()
        stmt = (
            insert(Instrument)
            .values(
                ticker=ticker,
                name=name or ticker,
                short_name=(name[:50] if name else ticker),
                isin=isin or None,
                currency_code=currency or None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["ticker"],
                set_={"isin": isin or None, "updated_at": now},
            )
        )
        self.session.execute(stmt)
        if existing is None:
            self._result.instruments_created += 1
        self._known_tickers.add(ticker)
        return ticker

    # ── Order import ───────────────────────────────────────────────────────

    def _import_order(self, row: dict, hm: dict[str, str], action: str) -> None:
        t212_id = _get(row, hm, "id")
        if not t212_id:
            self._result.skipped += 1
            return

        ticker = _get(row, hm, "ticker")
        isin = _get(row, hm, "isin")
        name = _get(row, hm, "name")
        currency = _get(row, hm, "currency (price / share)", "currency")
        side = "BUY" if action in _BUY_ACTIONS else "SELL"
        qty = _float(_get(row, hm, "no. of shares"))
        # Use GBP-equivalent price per share: Total / No. of shares
        total = _float(_get(row, hm, "total"))
        price_local = _float(_get(row, hm, "price / share", "price/share"))
        fill_price_gbp = (total / qty) if (total is not None and qty and qty > 0) else price_local
        filled_at = _parse_dt(_get(row, hm, "time (utc)", "time"))
        notes = _get(row, hm, "notes") or None

        ticker = self._ensure_instrument(isin, ticker, name, currency)

        stmt = (
            insert(Order)
            .values(
                id=t212_id,
                ticker=ticker,
                user_id=self.user_id,
                platform_id=self.platform_id,
                account=self.platform.account_type,
                quantity=qty,
                filled_quantity=qty,
                order_type=_order_type(action),
                side=side,
                status="FILLED",
                fill_price=fill_price_gbp,
                filled_at=filled_at,
                synced_at=datetime.datetime.utcnow(),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "id"],
                set_={
                    "platform_id": self.platform_id,
                    "account": self.platform.account_type,
                    "filled_quantity": qty,
                    "fill_price": fill_price_gbp,
                    "filled_at": filled_at,
                    "synced_at": datetime.datetime.utcnow(),
                },
            )
        )
        self.session.execute(stmt)
        self._result.orders += 1

    # ── Dividend import ────────────────────────────────────────────────────

    def _import_dividend(self, row: dict, hm: dict[str, str], action: str) -> None:
        t212_id = _get(row, hm, "id")
        if not t212_id:
            # T212 doesn't assign IDs to dividend rows; synthesise one from ticker + time
            ticker_raw = _get(row, hm, "ticker")
            time_raw = _get(row, hm, "time (utc)", "time")
            if ticker_raw and time_raw:
                t212_id = f"div_{ticker_raw}_{time_raw}"
            else:
                self._result.skipped += 1
                return

        ticker = _get(row, hm, "ticker")
        isin = _get(row, hm, "isin")
        name = _get(row, hm, "name")
        currency = _get(row, hm, "currency (price / share)", "currency")
        qty = _float(_get(row, hm, "no. of shares"))
        total = _float(_get(row, hm, "total"))
        price_per_share = _float(_get(row, hm, "price / share", "price/share"))
        paid_on = _parse_dt(_get(row, hm, "time (utc)", "time"))
        div_type = _derive_dividend_type(action)

        ticker = self._ensure_instrument(isin, ticker, name, currency)
        if not ticker:
            self._result.skipped += 1
            return

        stmt = (
            insert(DividendPayment)
            .values(
                reference=t212_id,
                ticker=ticker,
                platform_id=self.platform_id,
                account=self.platform.account_type,
                user_id=self.user_id,
                quantity=qty,
                amount=total,
                gross_amount_per_share=price_per_share,
                paid_on=paid_on,
                type=div_type,
                synced_at=datetime.datetime.utcnow(),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "reference"],
                set_={
                    "platform_id": self.platform_id,
                    "account": self.platform.account_type,
                    "amount": total,
                    "synced_at": datetime.datetime.utcnow(),
                },
            )
        )
        self.session.execute(stmt)
        self._result.dividends += 1

    # ── Cash transaction import ────────────────────────────────────────────

    def _import_transaction(self, row: dict, hm: dict[str, str], action: str) -> None:
        t212_id = _get(row, hm, "id")
        if not t212_id:
            self._result.skipped += 1
            return

        total = _float(_get(row, hm, "total"))
        date_time = _parse_dt(_get(row, hm, "time (utc)", "time"))
        notes = _get(row, hm, "notes") or None

        stmt = (
            insert(Transaction)
            .values(
                id=t212_id,
                user_id=self.user_id,
                platform_id=self.platform_id,
                type=_normalise_tx_type(action),
                amount=total,
                date_time=date_time,
                reference=t212_id,
                notes=notes,
                synced_at=datetime.datetime.utcnow(),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "id"],
                set_={
                    "platform_id": self.platform_id,
                    "amount": total,
                    "synced_at": datetime.datetime.utcnow(),
                },
            )
        )
        self.session.execute(stmt)
        self._result.cash_transactions += 1

    # ── Position rebuild ───────────────────────────────────────────────────

    def _rebuild_positions(self) -> None:
        """Recompute positions for this platform from the full order history."""
        rows = self.session.execute(_text("""
            SELECT
                ticker,
                SUM(CASE WHEN side = 'BUY'  THEN COALESCE(filled_quantity, 0) ELSE 0 END)
              - SUM(CASE WHEN side = 'SELL' THEN COALESCE(filled_quantity, 0) ELSE 0 END)
                AS net_qty,
                CASE
                  WHEN SUM(CASE WHEN side = 'BUY' THEN COALESCE(filled_quantity, 0) ELSE 0 END) > 0
                  THEN SUM(CASE WHEN side = 'BUY'
                                THEN COALESCE(fill_price, 0) * COALESCE(filled_quantity, 0)
                                ELSE 0 END)
                     / SUM(CASE WHEN side = 'BUY' THEN COALESCE(filled_quantity, 0) ELSE 0 END)
                  ELSE NULL
                END AS avg_buy_price
            FROM orders
            WHERE platform_id = :pid
              AND user_id     = :uid
              AND status IN ('FILLED', 'PARTIALLY_FILLED')
              AND ticker IS NOT NULL
            GROUP BY ticker
            HAVING (
                SUM(CASE WHEN side = 'BUY'  THEN COALESCE(filled_quantity, 0) ELSE 0 END)
              - SUM(CASE WHEN side = 'SELL' THEN COALESCE(filled_quantity, 0) ELSE 0 END)
            ) > 0.00001
        """), {"pid": self.platform_id, "uid": self.user_id}).fetchall()

        now = datetime.datetime.utcnow()
        for r in rows:
            stmt = (
                insert(Position)
                .values(
                    ticker=r.ticker,
                    platform_id=self.platform_id,
                    user_id=self.user_id,
                    account=self.platform.account_type,
                    quantity=float(r.net_qty),
                    average_price=float(r.avg_buy_price) if r.avg_buy_price else None,
                    last_synced_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["ticker", "platform_id", "user_id"],
                    set_={
                        "quantity": float(r.net_qty),
                        "average_price": float(r.avg_buy_price) if r.avg_buy_price else None,
                        "last_synced_at": now,
                    },
                )
            )
            self.session.execute(stmt)
            self._result.positions_upserted += 1

        # Remove positions that are now fully sold (net_qty ≤ 0)
        self.session.execute(_text("""
            DELETE FROM positions
            WHERE platform_id = :pid
              AND user_id     = :uid
              AND ticker NOT IN (
                SELECT ticker
                FROM orders
                WHERE platform_id = :pid
                  AND user_id     = :uid
                  AND status IN ('FILLED', 'PARTIALLY_FILLED')
                GROUP BY ticker
                HAVING (
                    SUM(CASE WHEN side = 'BUY'  THEN COALESCE(filled_quantity, 0) ELSE 0 END)
                  - SUM(CASE WHEN side = 'SELL' THEN COALESCE(filled_quantity, 0) ELSE 0 END)
                ) > 0.00001
              )
        """), {"pid": self.platform_id, "uid": self.user_id})


# ── Helpers ────────────────────────────────────────────────────────────────

def _derive_dividend_type(action: str) -> str:
    a = action.lower()
    if "manufactured" in a:
        return "DIVIDEND_MANUFACTURED_PAYMENT"
    if "equalisation" in a:
        return "DIVIDEND_EQUALISATION"
    if "property" in a:
        return "DIVIDEND_PROPERTY_INCOME"
    if "return of capital" in a:
        return "RETURN_OF_CAPITAL"
    return "DIVIDEND"


_TX_TYPE_MAP = {
    "deposit": "DEPOSIT",
    "withdrawal": "WITHDRAWAL",
    "interest on cash": "INTEREST",
    "lending interest": "LENDING_INTEREST",
    "currency conversion": "CURRENCY_CONVERSION",
    "fee": "FEE",
    "spend": "SPEND",
    "receive": "RECEIVE",
    "withholding tax": "WITHHOLDING_TAX",
    "result": "RESULT",
    "free stock": "FREE_STOCK",
}


def _normalise_tx_type(action: str) -> str:
    return _TX_TYPE_MAP.get(action.lower(), action.upper().replace(" ", "_"))
