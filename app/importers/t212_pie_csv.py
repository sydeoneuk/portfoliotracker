"""
T212 Pie CSV importer.

T212 allows exporting individual pie snapshots as CSV (one pie at a time).
This importer creates / updates a Pie record and replaces its holdings with
the snapshot, then runs a reconciliation against the positions table so the
user can see where pie-held quantities diverge from transaction-derived
quantities.

Expected CSV columns (T212 standard pie export — flexible matching):
  Ticker, Name, ISIN, Shares / Units / Owned Quantity,
  Target Allocation (%), Current Allocation (%),
  Invested value, Current value, Result (%)
"""

import csv
import datetime
import io
import math
import zlib
from dataclasses import dataclass, field
from typing import IO

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Instrument, Platform, Position
from app.models.pie import Pie, PieHolding
from app.importers.t212_csv import _normalise_headers, _get, _float
from app.identity import T212_PIE_CSV, require_resolved_identity


# ── Result dataclass ───────────────────────────────────────────────────────

@dataclass
class PieImportResult:
    pie_name: str = ""
    holdings_imported: int = 0
    holdings_replaced: int = 0
    instruments_created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    reconciliation: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.holdings_imported} holdings imported"]
        if self.holdings_replaced:
            parts.append(f"{self.holdings_replaced} previous holdings replaced")
        if self.instruments_created:
            parts.append(f"{self.instruments_created} new instruments")
        if self.skipped:
            parts.append(f"{self.skipped} rows skipped")
        return f"Pie '{self.pie_name}': " + ", ".join(parts) + "."


# ── Importer ───────────────────────────────────────────────────────────────

class T212PieCsvImporter:
    def __init__(
        self,
        session: Session,
        platform: Platform,
        user_id: int,
        pie_name: str,
        *, managed=False,
    ):
        self.managed = managed
        self.session = session
        self.platform = platform
        self.platform_id = platform.id
        self.user_id = user_id
        self.pie_name = pie_name.strip()
        self._result = PieImportResult(pie_name=self.pie_name)
        self._known_tickers: set[str] = set()
        self._pending_instruments: dict[str, tuple] = {}

    # ── Public entry point ─────────────────────────────────────────────────

    def import_file(self, file_obj: IO[bytes] | IO[str]) -> PieImportResult:
        try:
            return self._import_file(file_obj)
        except Exception as exc:
            if self.managed:
                raise
            self.session.rollback()
            self._known_tickers.clear()
            self._pending_instruments.clear()
            self._result = PieImportResult(
                pie_name=self.pie_name,
                errors=[f"{exc}. Import rolled back; no changes saved."],
            )
            return self._result

    def _import_file(self, file_obj: IO[bytes] | IO[str]) -> PieImportResult:
        raw = file_obj.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")

        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            self._result.errors.append("CSV has no headers — is the file empty?")
            return self._result

        headers_map = _normalise_headers(list(reader.fieldnames))
        rows = list(reader)

        if not rows:
            self._result.errors.append("CSV has no data rows.")
            return self._result

        if not any(k in headers_map for k in ("slice", "ticker", "symbol", "code")) or not any(
            k in headers_map for k in ("shares", "units", "owned quantity", "no. of shares", "quantity", "number of shares")
        ):
            raise ValueError("Pie CSV requires ticker and quantity columns")

        # Validate every row before creating a pie, writing instruments, or
        # replacing existing holdings. A partial snapshot cannot be authoritative.
        new_holdings = []
        seen = set()
        for row_num, row in enumerate(rows, start=2):
            try:
                if None in row:
                    raise ValueError("Too many CSV cells")
                holding = self._parse_row(row, headers_map, None)
            except ValueError as exc:
                raise ValueError(f"Row {row_num}: {exc}") from exc
            if holding is not None:
                if holding.ticker in seen:
                    raise ValueError(f"Row {row_num}: duplicate holding {holding.ticker}")
                seen.add(holding.ticker)
                new_holdings.append(holding)
        if not new_holdings:
            raise ValueError("No valid holdings found; existing pie retained")

        pie = self._ensure_pie()

        # Remove stale holdings (replace strategy — CSV is authoritative snapshot)
        old_count = (
            self.session.query(PieHolding)
            .filter_by(user_id=self.user_id, pie_id=pie.pk)
            .count()
        )
        self.session.query(PieHolding).filter_by(
            user_id=self.user_id, pie_id=pie.pk
        ).delete()
        self._result.holdings_replaced = old_count

        for h in new_holdings:
            self._ensure_instrument(*self._pending_instruments[h.ticker])
            h.pie_id = pie.pk
            self.session.add(h)

        self._result.holdings_imported = len(new_holdings)

        pie.last_synced_at = datetime.datetime.utcnow()
        self.session.flush()

        # Reconciliation against positions
        self._reconcile(new_holdings)

        if not self.managed:
            self.session.commit()
        return self._result

    # ── Pie upsert ─────────────────────────────────────────────────────────

    def _ensure_pie(self) -> Pie:
        existing = (
            self.session.query(Pie)
            .filter_by(
                user_id=self.user_id,
                platform_id=self.platform_id,
                name=self.pie_name,
                source_namespace=T212_PIE_CSV,
            )
            .first()
        )
        if existing:
            return existing

        ambiguous = self.session.query(Pie).filter(
            Pie.user_id == self.user_id, Pie.platform_id == self.platform_id,
            Pie.name == self.pie_name, Pie.source_namespace != T212_PIE_CSV,
        ).first()
        if ambiguous is not None:
            raise ValueError("Existing pie source is ambiguous; reconcile its identity before importing a snapshot")

        # Generate a stable synthetic T212-like ID for CSV-sourced pies.
        # Prefix "csv:" ensures no collision with real T212 API IDs.
        synthetic_id = (
            zlib.crc32(f"csv:{self.platform_id}:{self.pie_name}".encode()) & 0x7FFFFFFF
        )

        # Handle the unlikely case where the synthetic ID conflicts with
        # another pie for this user.
        if self.session.query(Pie).filter_by(user_id=self.user_id, platform_id=self.platform_id,
                                            source_namespace=T212_PIE_CSV, id=synthetic_id).first():
            synthetic_id = (synthetic_id + 1) & 0x7FFFFFFF
        require_resolved_identity(self.session, Pie, self.user_id, "id", synthetic_id, self.platform_id)

        now = datetime.datetime.utcnow()
        pie = Pie(
            source_namespace=T212_PIE_CSV,
            id=synthetic_id,
            user_id=self.user_id,
            platform_id=self.platform_id,
            account=self.platform.account_type,
            name=self.pie_name,
            last_synced_at=now,
            created_at=now,
        )
        self.session.add(pie)
        self.session.flush()
        return pie

    # ── Row parsing ────────────────────────────────────────────────────────

    def _parse_row(
        self, row: dict, hm: dict[str, str], pie_pk: int | None
    ) -> PieHolding | None:
        # T212 pie CSV uses "Slice" as the ticker column, with a trailing
        # "Total" summary row that must be skipped.
        ticker = _get(row, hm, "slice", "ticker", "symbol", "code")
        if ticker.lower() == "total" or not any(v and str(v).strip() for v in row.values()):
            self._result.skipped += 1
            return None
        if not ticker:
            raise ValueError("Missing holding ticker")

        # Quantity — try several common column names T212 uses
        qty_raw = _get(
            row, hm,
            "shares", "units", "owned quantity", "no. of shares",
            "quantity", "number of shares",
        )
        qty = _float(qty_raw)
        if qty is None or not math.isfinite(qty) or qty < 0:
            raise ValueError(f"Invalid quantity for {ticker}")
        if qty == 0:
            self._result.skipped += 1
            return None

        # Optional enrichment fields
        name = _get(row, hm, "name", "instrument", "company", "description")
        isin = _get(row, hm, "isin")
        currency = _get(row, hm, "currency (price / share)", "currency")

        def number(*keys: str) -> float | None:
            raw = _get(row, hm, *keys)
            if raw.lower() in {"", "-", "—", "n/a"}:
                return None
            value = _float(raw)
            if value is None or not math.isfinite(value):
                raise ValueError(f"Invalid {keys[0]} for {ticker}")
            return value

        def fraction(*keys: str) -> float | None:
            value = number(*keys)
            header = next((hm[k] for k in keys if k in hm), "")
            if value is not None and ("%" in header or abs(value) > 1):
                return value / 100
            return value

        expected_share = fraction("target allocation", "target allocation (%)", "target (%)", "target", "expected share")
        current_share = fraction("current allocation", "current allocation (%)", "current (%)", "current", "current share")
        avg_invested = number("price (avg. invested value)", "average buy price", "avg cost", "invested amount")
        avg_value = number("current value", "price (avg. value)", "value")
        avg_result = number("result", "return", "p&l", "ppl")
        avg_result_coef = fraction("result (%)", "return (%)", "result coef")

        # Ensure instrument exists
        self._pending_instruments[ticker] = (isin, ticker, name, currency)

        return PieHolding(
            user_id=self.user_id,
            pie_id=pie_pk,
            ticker=ticker,
            owned_quantity=qty,
            expected_share=expected_share,
            current_share=current_share,
            price_avg_invested_value=avg_invested,
            price_avg_value=avg_value,
            price_avg_result=avg_result,
            price_avg_result_coef=avg_result_coef,
            synced_at=datetime.datetime.utcnow(),
        )

    # ── Instrument helper ──────────────────────────────────────────────────

    def _ensure_instrument(
        self, isin: str, ticker: str, name: str, currency: str
    ) -> None:
        if not ticker or ticker in self._known_tickers:
            return
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
                set_={"isin": func.coalesce(Instrument.isin, isin or None), "updated_at": now},
            )
        )
        self.session.execute(stmt)
        if existing is None:
            self._result.instruments_created += 1
        self._known_tickers.add(ticker)

    # ── Reconciliation ─────────────────────────────────────────────────────

    def _reconcile(self, holdings: list[PieHolding]) -> None:
        """Compare pie-held quantities against position quantities.

        The position quantity is derived from the full transaction history
        (all buys/sells for that ticker on that platform). The pie quantity
        is what was exported from the T212 pie snapshot. Differences arise
        from fractional share rounding, timing of intra-day snapshots, or
        manual share transfers.
        """
        # Aggregate all pie holdings for this platform, not just this pie,
        # so we can compare the total pie allocation per ticker.
        from sqlalchemy import text
        rows = self.session.execute(text("""
            SELECT ph.ticker, SUM(ph.owned_quantity) AS total_pie_qty
            FROM pie_holdings ph
            JOIN pies pie ON pie.pk = ph.pie_id
            WHERE pie.user_id = :user_id
              AND pie.platform_id = :platform_id
            GROUP BY ph.ticker
        """), {"user_id": self.user_id, "platform_id": self.platform_id}).fetchall()

        pie_totals = {r.ticker: float(r.total_pie_qty) for r in rows}

        for holding in holdings:
            ticker = holding.ticker
            pie_qty = pie_totals.get(ticker, 0.0)
            position = (
                self.session.query(Position)
                .filter_by(
                    user_id=self.user_id,
                    platform_id=self.platform_id,
                    ticker=ticker,
                )
                .first()
            )
            pos_qty = float(position.quantity) if position else 0.0
            diff = pie_qty - pos_qty

            if abs(diff) < 0.0001:
                continue  # reconciled

            self._result.reconciliation.append({
                "ticker": ticker,
                "pie_qty": round(pie_qty, 6),
                "position_qty": round(pos_qty, 6),
                "diff": round(diff, 6),
                "note": (
                    "No position found — run transaction CSV import first"
                    if position is None
                    else "Pie holdings exceed position quantity (manual transfer or rounding?)"
                    if diff > 0
                    else "Position quantity exceeds pie holdings (shares held outside pies?)"
                ),
            })
