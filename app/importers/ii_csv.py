"""
Interactive Investor (ii) holdings CSV importer.

ii allows exporting the current portfolio as a CSV snapshot.  This is a
positions import (not transaction history), so it replaces all positions for
the platform on each import.

Expected columns (ii standard export):
  Symbol, Name, Qty, Price, Day Gain/Loss, Day Gain/Loss %,
  Market Value £, Market Value, Book Cost, Gain/Loss, Gain/Loss %,
  Average Price

Price format variants:
  118.90p  → GBX (pence), value 118.90
  £7.596   → GBP, value 7.596
  $6.467   → USD, value 6.467
"""

import csv
import datetime
import io
import math
import re
from dataclasses import dataclass, field
from typing import IO

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Instrument, Platform, Position
from app.cost_basis import broker_cost_values


# ── Helpers ────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return (s or "").strip().lstrip("﻿")


def _parse_ii_value(s: str) -> tuple[float | None, str | None]:
    """Parse an ii price/value string into (amount, currency_code).

    Returns (None, None) for empty or unparseable values.
    Currency codes: GBX (pence), GBP, USD — others passed through.
    """
    s = _clean(s).replace(",", "").replace("\xa0", "")
    if not s or s in ("—", "-", "N/A"):
        return None, None
    if s.endswith("p"):
        val = _safe_float(s[:-1])
        return val, "GBX"
    if s.startswith("£"):
        val = _safe_float(s[1:])
        return val, "GBP"
    if s.startswith("$"):
        val = _safe_float(s[1:])
        return val, "USD"
    if s.startswith("€"):
        val = _safe_float(s[1:])
        return val, "EUR"
    # Plain number — return with no currency hint
    return _safe_float(s), None


def _safe_float(s: str) -> float | None:
    s = _clean(s).replace(",", "").replace("%", "")
    s = s.lstrip("£$€").strip()
    if not s:
        return None
    # Strip trailing currency indicators not caught above
    s = re.sub(r"[a-zA-Z]+$", "", s).strip()
    try:
        return float(s)
    except ValueError:
        return None


# ── Result dataclass ───────────────────────────────────────────────────────

@dataclass
class IiImportResult:
    positions_upserted: int = 0
    positions_deleted: int = 0
    instruments_created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    columns_detected: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.positions_upserted} positions imported"]
        if self.positions_deleted:
            parts.append(f"{self.positions_deleted} stale positions removed")
        if self.instruments_created:
            parts.append(f"{self.instruments_created} new instruments")
        if self.skipped:
            parts.append(f"{self.skipped} rows skipped")
        return ", ".join(parts) + "."


# ── Importer ───────────────────────────────────────────────────────────────

class IiCsvImporter:
    def __init__(self, session: Session, platform: Platform, user_id: int, *, managed=False):
        self.managed = managed
        self.session = session
        self.platform = platform
        self.platform_id = platform.id
        self.user_id = user_id
        self._result = IiImportResult()
        self._in_totals_footer = False

    def import_file(self, file_obj: IO[bytes] | IO[str]) -> IiImportResult:
        try:
            return self._import_file(file_obj)
        except Exception as exc:
            if self.managed:
                raise
            self.session.rollback()
            self._result = IiImportResult(
                columns_detected=self._result.columns_detected,
                errors=[f"{exc}. Import rolled back; no changes saved."],
            )
            return self._result

    def _import_file(self, file_obj: IO[bytes] | IO[str]) -> IiImportResult:
        raw = file_obj.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
        # Strip multiple leading BOMs that ii sometimes adds
        raw = raw.lstrip("﻿")

        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            self._result.errors.append("CSV has no headers — is the file empty?")
            return self._result

        # Normalise header names: strip whitespace and £ suffix variants
        norm_map: dict[str, str] = {}
        for h in reader.fieldnames:
            key = _clean(h).lower().replace(" £", "").replace("£", "").strip()
            norm_map[h] = key

        self._result.columns_detected = list(norm_map.values())
        fields = list(norm_map.values())
        if not any(k in fields for k in ("symbol", "ticker", "epic", "code")) or "qty" not in fields or "price" not in fields:
            raise ValueError("Holdings CSV requires Symbol, Qty and Price columns")
        for key in ("symbol", "ticker", "epic", "code", "qty", "price", "average price"):
            if fields.count(key) > 1:
                raise ValueError(f"Ambiguous CSV column: {key}")
        rows = list(reader)
        if not rows:
            self._result.errors.append("CSV has no data rows.")
            return self._result

        # Parse valid position rows first (so we can do a clean replace)
        new_positions: list[dict] = []
        seen = set()
        for row_num, row in enumerate(rows, start=2):
            try:
                if None in row:
                    raise ValueError("Too many CSV cells")
                parsed = self._parse_row(row, norm_map)
            except ValueError as exc:
                raise ValueError(f"Row {row_num}: {exc}") from exc
            if parsed is not None:
                if parsed["ticker"] in seen:
                    raise ValueError(f"Row {row_num}: duplicate holding {parsed['ticker']}")
                seen.add(parsed["ticker"])
                new_positions.append(parsed)

        if not new_positions:
            skipped = self._result.skipped
            first_headers = list(norm_map.values())[:5]
            self._result.errors.append(
                f"No valid position rows found ({skipped} rows skipped). "
                f"Headers detected: {first_headers}. "
                "Check the file is an ii holdings export (Symbol, Qty, Price columns required)."
            )
            return self._result

        # Replace strategy: remove positions no longer in the export
        incoming_tickers = {p["ticker"] for p in new_positions}
        existing = (
            self.session.query(Position)
            .filter_by(user_id=self.user_id, platform_id=self.platform_id)
            .all()
        )
        for pos in existing:
            if pos.ticker not in incoming_tickers:
                self.session.delete(pos)
                self._result.positions_deleted += 1

        now = datetime.datetime.utcnow()
        for p in new_positions:
            self._ensure_instrument(p["ticker"], p.get("name"), p.get("currency"))
            recorded = self.session.get(Instrument, p['ticker'])
            recorded_currency = recorded.currency_code if recorded is not None else None
            if recorded_currency == 'GBp':
                recorded_currency = 'GBX'
            if recorded_currency and p['currency'] and recorded_currency != p['currency']:
                scales = {('GBP', 'GBX'): 100, ('GBX', 'GBP'): 0.01}
                scale = scales.get((p['currency'], recorded_currency))
                if scale is None:
                    raise ValueError(f"Quote currency conflicts with recorded listing for {p['ticker']}; reconcile instrument metadata first")
                for field in ('current_price', 'average_price'):
                    if p[field] is not None:
                        p[field] *= scale
            basis = p['basis']
            stmt = (
                pg_insert(Position)
                .values(
                    ticker=p["ticker"],
                    platform_id=self.platform_id,
                    user_id=self.user_id,
                    account=self.platform.account_type,
                    quantity=p["quantity"],
                    average_price=p.get("average_price"),
                    current_price=p.get("current_price"),
                    **basis,
                    last_synced_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["ticker", "platform_id", "user_id"],
                    set_={
                        "quantity": p["quantity"],
                        "average_price": p.get("average_price"),
                        "current_price": p.get("current_price"),
                        **basis,
                        "last_synced_at": now,
                    },
                )
            )
            self.session.execute(stmt)
            self._result.positions_upserted += 1

        if not self.managed:
            self.session.commit()
        return self._result

    # ── Row parsing ────────────────────────────────────────────────────────

    def _parse_row(self, row: dict, norm_map: dict[str, str]) -> dict | None:
        def get(key: str) -> str:
            for orig, norm in norm_map.items():
                if norm == key:
                    return _clean(row.get(orig, ""))
            return ""

        ticker = get("symbol") or get("ticker") or get("epic") or get("code")
        if not ticker and not get('name') and not get('qty') and not get('average price'):
            footer = get('price')
            if footer.lower() in {'total', 'totals'} and all(
                not _clean(value) for original, value in row.items() if norm_map.get(original) != 'price'
            ):
                self._in_totals_footer = True
                self._result.skipped += 1
                return None
            if self._in_totals_footer and footer in {'GBP', 'USD', 'EUR', 'GBX', 'CAD', 'AUD', 'CHF', 'JPY', 'HKD', 'SGD', 'NZD', 'SEK', 'NOK', 'DKK', 'ZAR'}:
                self._result.skipped += 1
                return None
        if self._in_totals_footer and ticker:
            raise ValueError('Holding found after totals footer; snapshot coverage is ambiguous')
        if ticker.lower() == "total" or not any(v and str(v).strip() for v in row.values()):
            self._result.skipped += 1
            return None
        if not ticker:
            raise ValueError("Missing holding symbol")

        name = get("name")
        qty_raw = get("qty")
        qty = _safe_float(qty_raw)
        if qty is None or not math.isfinite(qty) or qty < 0:
            raise ValueError(f"Invalid quantity for {ticker}")
        if qty == 0:
            self._result.skipped += 1
            return None

        # Current price — infer currency from format
        price_raw = get("price")
        current_price, currency = _parse_ii_value(price_raw)
        if price_raw.lower() not in {"", "-", "—", "n/a"} and (current_price is None or not math.isfinite(current_price) or current_price < 0):
            raise ValueError(f"Invalid price for {ticker}")

        # ii can express cost in account currency while quoting in USD/GBX.
        avg_raw = get("average price")
        avg_price, avg_ccy = _parse_ii_value(avg_raw)
        if avg_raw.lower() not in {"", "-", "—", "n/a"} and (avg_price is None or not math.isfinite(avg_price) or avg_price < 0):
            raise ValueError(f"Invalid average price for {ticker}")
        # Zero average price means no cost basis (transferred in) — store None
        if avg_price is not None and avg_price == 0.0:
            avg_price = None
        basis = broker_cost_values(qty, avg_price, avg_ccy)
        book_raw = get('book cost')
        book_cost, book_ccy = _parse_ii_value(book_raw)
        if book_raw.lower() not in {'', '-', '—', 'n/a'}:
            if book_cost is None or not math.isfinite(book_cost) or book_cost < 0:
                raise ValueError(f'Invalid book cost for {ticker}')
            if book_cost > 0 and book_ccy:
                basis = dict(cost_basis_amount=book_cost, cost_basis_currency=book_ccy,
                    cost_basis_status='reported', cost_basis_source='broker', cost_basis_reason='Book cost from ii statement')
            elif book_cost == 0:
                basis = broker_cost_values(qty, None, book_ccy)
        # The deprecated average_price remains in quote units only. Explicit
        # cost fields preserve a differently denominated broker cost unchanged.
        native_avg = avg_price
        if avg_price is not None and avg_ccy != currency:
            if avg_ccy == 'GBP' and currency == 'GBX':
                native_avg = avg_price * 100
            elif avg_ccy == 'GBX' and currency == 'GBP':
                native_avg = avg_price / 100
            else:
                native_avg = None

        return {
            "ticker": ticker,
            "name": name,
            "quantity": qty,
            "currency": currency,
            "current_price": current_price,
            "average_price": native_avg,
            "basis": basis,
        }

    # ── Instrument upsert ──────────────────────────────────────────────────

    def _ensure_instrument(self, ticker: str, name: str | None, currency: str | None) -> None:
        now = datetime.datetime.utcnow()
        # Derive the Yahoo Finance ticker for LSE-listed instruments (GBP/GBX priced).
        # This prevents yfinance enrichment from resolving the bare ticker to a US stock
        # that happens to share the same symbol (e.g. FWRG → First Watch Restaurant Group
        # instead of FWRG.L → Invesco FTSE All-World ETF).
        yf_candidate = None
        if (currency or "").upper() in ("GBP", "GBX"):
            yf_candidate = f"{ticker}.L"

        values: dict = dict(
            ticker=ticker,
            name=name or ticker,
            short_name=(name[:50] if name else ticker),
            currency_code=currency or None,
            created_at=now,
            updated_at=now,
        )
        if yf_candidate:
            values["yf_ticker"] = yf_candidate

        set_update: dict = {
            "name": name or ticker,
            "short_name": (name[:50] if name else ticker),
            "updated_at": now,
        }
        if yf_candidate:
            # Only set yf_ticker if not already present so manually-corrected values survive.
            set_update["yf_ticker"] = func.coalesce(Instrument.yf_ticker, yf_candidate)

        stmt = (
            pg_insert(Instrument)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["ticker"],
                set_=set_update,
            )
        )
        existing = self.session.get(Instrument, ticker)
        self.session.execute(stmt)
        if existing is None:
            self._result.instruments_created += 1
