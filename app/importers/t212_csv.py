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
from app.identity import T212, require_resolved_identity


import csv
import datetime
import io
import hashlib
import json
from decimal import Decimal, InvalidOperation
import re
from dataclasses import dataclass, field
from typing import IO

from sqlalchemy import text as _text
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.cost_basis import moving_average_cost
from app.models.order import TradeImportObservation
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
        key = h.strip().lstrip("\ufeff").lower()
        # Parentheses can describe the field, not its unit. In particular,
        # each Currency (...) column must retain its own identity.
        if key == "time (utc)":
            key = "time"
        elif not key.startswith("currency ("):
            key = re.sub(r"\s+\([a-z]{3}\)$", "", key)
        if key in result:
            raise ValueError(f"Ambiguous CSV headers: {result[key]!r} and {h!r}")
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
    try:
        parsed = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        pass
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
    warnings: list[str] = field(default_factory=list)

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
    def __init__(self, session: Session, platform: Platform, user_id: int, *, managed=False, rebuild_positions=True, replace_observations=False):
        self.managed = managed
        self.rebuild_positions = rebuild_positions
        self.replace_observations = replace_observations
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

        # A failed row must not discard preceding rows and then commit the
        # remainder as a successful import. Apply the whole file atomically.
        stage = "reading rows"
        try:
            headers_map = _normalise_headers(list(reader.fieldnames))
            for i, row in enumerate(reader, start=2):
                stage = f"Row {i}"
                self._process_row(row, headers_map)
            stage = "Rebuilding positions"
            self.session.flush()
            if self.rebuild_positions:
                self._rebuild_positions()
            else:
                self._result.warnings.append('Events retained; holdings authority unchanged because complete history was not selected.')
            stage = "Committing import"
            if not self.managed:
                self.session.commit()
        except Exception as exc:
            if self.managed:
                raise
            self.session.rollback()
            self._known_tickers.clear()
            self._seen_trade_ids = {}
            self._result = ImportResult(
                errors=[f"{stage}: {exc}. Import rolled back; no changes saved."]
            )
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
            if any(label in action for label in (
                "stock split", "stock transfer", "share transfer", "free stock", "free shares",
                "merger", "spin-off", "spinoff", "rights issue",
            )):
                raise ValueError("Security movement requires holdings/cost reconciliation; transaction projection not supported")
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
                set_={"isin": func.coalesce(Instrument.isin, isin or None), "updated_at": now},
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
        def number(*keys):
            raw = _get(row, hm, *keys)
            if not raw or raw.lower() in {"n/a", "-", "—"}:
                return None
            try:
                value = Decimal(raw.replace(",", "").replace("−", "-").replace("–", "-"))
            except InvalidOperation as exc:
                raise ValueError(f"Invalid {keys[0]}") from exc
            if not value.is_finite():
                raise ValueError(f"Invalid {keys[0]}")
            return value

        def field_currency(field):
            explicit = _get(row, hm, f"currency ({field})")
            if explicit:
                return explicit
            header = hm.get(field, "")
            match = re.search(r"\(([A-Z]{3})\)$", header)
            return match.group(1) if match else None

        qty = number("no. of shares")
        price_local = number("price / share", "price/share")
        total = number("total")
        filled_at = _parse_dt(_get(row, hm, "time (utc)", "time"))
        if not ticker or qty is None or qty <= 0 or filled_at is None:
            raise ValueError("Trade requires ticker, positive quantity and execution time")
        if price_local is not None and price_local < 0:
            raise ValueError("Execution price cannot be negative")
        currency = field_currency("price / share") or currency or None
        settlement_currency = field_currency("total")
        charges = []
        for field in ("stamp duty reserve tax", "transaction fee", "currency conversion fee", "finra fee", "fee", "withholding tax"):
            charge = number(field)
            if charge is not None:
                charges.append({"type": field, "amount": str(charge), "currency": field_currency(field)})
        raw_payload = {str(k): v for k, v in row.items()}
        payload_hash = hashlib.sha256(json.dumps(raw_payload, sort_keys=True).encode()).hexdigest()
        seen = getattr(self, "_seen_trade_ids", {})
        if t212_id in seen and seen[t212_id] != payload_hash:
            raise ValueError(f"Multiple different executions share ID {t212_id}; cannot collapse them safely")
        self._seen_trade_ids = {**seen, t212_id: payload_hash}
        ticker = self._ensure_instrument(isin, ticker, name, currency or "")
        now = datetime.datetime.utcnow()
        values = dict(
            execution_price=price_local, execution_currency=currency,
            settlement_amount=total, settlement_currency=settlement_currency,
            execution_fx_rate=number("exchange rate"), charges=charges,
        )
        require_resolved_identity(self.session, Order, self.user_id, "id", t212_id, self.platform_id)
        stmt = insert(Order).values(
            source_namespace=T212, id=t212_id, ticker=ticker, user_id=self.user_id, platform_id=self.platform_id,
            account=self.platform.account_type, quantity=qty, filled_quantity=qty,
            order_type=_order_type(action), side=side, status="FILLED",
            fill_price=price_local, filled_at=filled_at, synced_at=now, **values,
        ).on_conflict_do_update(
            index_elements=["user_id", "platform_id", "source_namespace", "id"],
            set_={**values, "filled_quantity": qty, "filled_at": filled_at, "synced_at": now,
                  "status": "FILLED", "side": side},
            where=(Order.platform_id == self.platform_id) & (Order.ticker == ticker) & ((Order.side == side) | Order.side.is_(None)),
        ).returning(Order.pk)
        order_pk = self.session.execute(stmt).scalar_one_or_none()
        if order_pk is None:
            raise ValueError(f"Trade ID {t212_id} belongs to another account or instrument; no records changed")
        self.session.execute(insert(TradeImportObservation).values(
            order_pk=order_pk, user_id=self.user_id, payload_hash=payload_hash,
            payload=raw_payload, imported_at=now,
        ).on_conflict_do_nothing(index_elements=["order_pk", "payload_hash"]))
        self._result.orders += 1

    # ── Dividend import ────────────────────────────────────────────────────

    def _import_dividend(self, row: dict, hm: dict[str, str], action: str) -> None:
        t212_id = _get(row, hm, "id")
        if not t212_id:
            # T212 doesn't assign IDs to dividend rows; synthesise one that is
            # unique per platform so ISA and SIPP dividends for the same ticker
            # on the same day don't collide on the (user_id, reference) constraint.
            ticker_raw = _get(row, hm, "ticker")
            time_raw = _get(row, hm, "time (utc)", "time")
            if ticker_raw and time_raw:
                t212_id = f"div_{self.platform_id}_{ticker_raw}_{time_raw}"
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

        require_resolved_identity(self.session, DividendPayment, self.user_id, "reference", t212_id, self.platform_id)
        stmt = (
            insert(DividendPayment)
            .values(
                source_namespace=T212, reference=t212_id,
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
                index_elements=["user_id", "platform_id", "source_namespace", "reference"],
                set_={
                    "platform_id": self.platform_id,
                    "account": self.platform.account_type,
                    "amount": total,
                    "paid_on": paid_on,
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

        require_resolved_identity(self.session, Transaction, self.user_id, "id", t212_id, self.platform_id)
        stmt = (
            insert(Transaction)
            .values(
                source_namespace=T212, id=t212_id,
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
                index_elements=["user_id", "platform_id", "source_namespace", "id"],
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
        """Project chronological costs without overwriting broker or legacy values."""
        rows = self.session.query(Order).filter(
            Order.platform_id == self.platform_id, Order.user_id == self.user_id,
            Order.source_namespace == T212,
            Order.status.in_(["FILLED", "PARTIALLY_FILLED"]), Order.ticker.isnot(None),
        ).all()
        grouped = {}
        for trade in rows:
            grouped.setdefault(trade.ticker, []).append(trade)
        if self.replace_observations:
            current = self.session.query(Position).filter_by(user_id=self.user_id, platform_id=self.platform_id).all()
            missing = [p.ticker for p in current if p.quantity and p.ticker not in grouped]
            if missing:
                raise ValueError('Declared complete history does not explain existing holdings: ' + ', '.join(missing))
        capital_returns = {r.ticker for r in self.session.query(DividendPayment).filter(
            DividendPayment.user_id == self.user_id, DividendPayment.platform_id == self.platform_id,
            DividendPayment.type == "RETURN_OF_CAPITAL",
        ).all()}
        now = datetime.datetime.utcnow()
        for ticker, trades in grouped.items():
            basis = moving_average_cost(trades)
            if ticker in capital_returns:
                from dataclasses import replace
                basis = replace(basis, amount=None, reason="Return of capital requires cost reconciliation")
            existing = self.session.get(Position, (ticker, self.platform_id, self.user_id))
            # Holdings/pie observations and legacy rows retain authority until
            # explicitly reconciled. An incomplete history must not shrink them.
            if existing is not None and existing.cost_basis_source != "transactions" and not self.replace_observations:
                if existing.cost_basis_source != "broker" and basis.quantity_reconciled and (
                    abs(Decimal(str(existing.quantity or 0)) - basis.quantity) <= Decimal("0.00000001")
                ):
                    existing.cost_basis_amount = basis.amount
                    existing.cost_basis_currency = basis.currency
                    existing.cost_basis_status = "estimated" if basis.amount is not None else "unknown"
                    existing.cost_basis_source = "reconciled"
                    existing.cost_basis_reason = basis.reason or "Supplied trades match preserved quantity; opening coverage unverified"
                elif existing.cost_basis_source != "broker":
                    self._result.warnings.append(f"{ticker}: existing holding retained; transaction quantities need reconciliation")
                continue
            if not basis.quantity_reconciled:
                if self.replace_observations:
                    raise ValueError(f'{ticker}: complete-history quantity cannot be reconciled: {basis.reason}')
                self._result.warnings.append(f"{ticker}: {basis.reason}; existing quantity retained")
                if existing is not None:
                    existing.cost_basis_amount = None
                    existing.cost_basis_status = "unknown"
                    existing.cost_basis_reason = basis.reason
                continue
            if basis.quantity == 0:
                if existing is not None:
                    self.session.delete(existing)
                continue
            values = dict(
                quantity=basis.quantity, cost_basis_amount=basis.amount,
                cost_basis_currency=basis.currency,
                cost_basis_status="estimated" if basis.amount is not None else "unknown",
                cost_basis_source="transactions",
                cost_basis_reason=basis.reason or "Moving average of supplied history; opening coverage unverified",
                last_synced_at=now,
            )
            self.session.execute(insert(Position).values(
                ticker=ticker, platform_id=self.platform_id, user_id=self.user_id,
                account=self.platform.account_type, **values,
            ).on_conflict_do_update(
                index_elements=["ticker", "platform_id", "user_id"], set_=values,
            ))
            self._result.positions_upserted += 1


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
