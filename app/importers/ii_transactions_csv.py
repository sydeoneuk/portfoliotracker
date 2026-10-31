"""
Interactive Investor (ii) transactions CSV importer.

Parses ii account transaction history exports and upserts records into
DividendPayment and Transaction tables.

Expected columns (ii transactions export, DD/MM/YYYY dates, £ amounts):
  Date, Settlement Date, Symbol, Sedol, Quantity, Price,
  Description, Reference, Debit, Credit, Running Balance

Row types handled:
  "Div {qty}   {name}"         → DividendPayment
  "GROSS INTEREST"             → Transaction (INTEREST)
  "... Mandatory Redemption"   → Transaction (BOND_REDEMPTION)
  Symbol + Reference provided  → Transaction (TRADE_BUY / TRADE_SELL)
  "Trf from ..."               → Transaction (TRANSFER_IN)
  "Trf to ..."                 → Transaction (TRANSFER_OUT)
  "CASHBACK..."                → Transaction (CASHBACK)
  "Debit card payment" / deposit-like phrases → Transaction (DEPOSIT)
  Other                        → Transaction (OTHER)

Trades are stored as cash transactions (not Orders) because positions for
ii accounts are managed by the separate holdings-CSV importer.
"""

import csv
import datetime
import io
import re
from dataclasses import dataclass, field
from typing import IO

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import DividendPayment, Instrument, Platform, Transaction


# ── Helpers ────────────────────────────────────────────────────────────────

_NA_VALS = {"n/a", "-", ""}

_DIV_RE = re.compile(r"^[Dd]iv\s+(\d+(?:\.\d+)?)\s+(.*)", re.DOTALL)


def _clean(s: str) -> str:
    """Strip BOMs and whitespace."""
    return (s or "").strip().lstrip("﻿").strip()


def _na(s: str) -> str | None:
    v = _clean(s)
    return None if v.lower() in _NA_VALS else v


def _parse_amount(s: str) -> float | None:
    v = _na(s)
    if v is None:
        return None
    v = v.replace("£", "").replace(",", "").strip()
    try:
        return float(v)
    except ValueError:
        return None


def _parse_date(s: str) -> datetime.datetime | None:
    v = _na(s)
    if v is None:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _classify(desc: str, symbol: str | None, ref: str | None,
              debit: float | None, credit: float | None) -> str:
    dl = desc.lower().strip()

    if _DIV_RE.match(desc):
        return "dividend"

    if "mandatory redemption" in dl or "maturity" in dl:
        return "bond_redemption"

    if dl == "gross interest":
        return "interest"

    if dl.startswith("trf from") or "transfer in" in dl:
        return "transfer_in"

    if dl.startswith("trf to") or "transfer out" in dl:
        return "transfer_out"

    if dl.startswith("cashback"):
        return "cashback"

    if "debit card" in dl or "cash deposit" in dl:
        return "deposit"

    # Trade: has a real symbol and a provided ii reference
    if symbol and ref:
        return "trade_buy" if (debit is not None) else "trade_sell"

    return "other"


def _synth_id(platform_id: int, row_type: str, date: datetime.datetime,
              amount: float | None) -> str:
    date_str = date.strftime("%Y-%m-%d")
    pence = round((amount or 0.0) * 100)
    return f"ii_{platform_id}_{row_type}_{date_str}_{pence}"


# ── Result ─────────────────────────────────────────────────────────────────

@dataclass
class IiTransactionsImportResult:
    dividends_upserted: int = 0
    transactions_upserted: int = 0
    instruments_created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.dividends_upserted:
            parts.append(f"{self.dividends_upserted} dividend(s)")
        if self.transactions_upserted:
            parts.append(f"{self.transactions_upserted} transaction(s)")
        if self.instruments_created:
            parts.append(f"{self.instruments_created} new instrument(s)")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return "Imported: " + ", ".join(parts) if parts else "Nothing imported."


# ── Importer ───────────────────────────────────────────────────────────────

class IiTransactionsCsvImporter:
    """Import an ii transactions CSV export into DividendPayment / Transaction."""

    def __init__(self, session: Session, platform: Platform, user_id: int):
        self._db = session
        self.platform = platform
        self.platform_id = platform.id
        self.user_id = user_id
        self._result = IiTransactionsImportResult()

    # ── Public ─────────────────────────────────────────────────────────────

    def import_file(self, file_obj: IO[bytes]) -> IiTransactionsImportResult:
        raw = file_obj.read()
        # Strip any leading BOMs / whitespace before the header
        text = raw.decode("utf-8-sig", errors="replace").lstrip("﻿").strip()
        reader = csv.DictReader(io.StringIO(text))

        # Normalise header names: strip BOM/whitespace from each key
        if reader.fieldnames:
            reader.fieldnames = [_clean(k) for k in reader.fieldnames]

        for row_num, row in enumerate(reader, start=2):
            # Re-key with cleaned names (csv.DictReader uses original fieldnames)
            cleaned = {_clean(k): _clean(v) for k, v in row.items() if k is not None}
            try:
                self._process_row(cleaned, row_num)
            except Exception as exc:
                self._result.errors.append(f"Row {row_num}: {exc}")

        self._db.flush()
        return self._result

    # ── Private ────────────────────────────────────────────────────────────

    def _process_row(self, row: dict, row_num: int) -> None:
        date = _parse_date(row.get("Date", ""))
        if date is None:
            self._result.skipped += 1
            return

        symbol = _na(row.get("Symbol", ""))
        ref = _na(row.get("Reference", ""))
        desc = _clean(row.get("Description", ""))
        debit = _parse_amount(row.get("Debit", ""))
        credit = _parse_amount(row.get("Credit", ""))

        row_type = _classify(desc, symbol, ref, debit, credit)

        if row_type == "dividend":
            self._import_dividend(row, desc, symbol, date, credit)
        else:
            self._import_transaction(row_type, desc, symbol, ref, date, debit, credit)

    # ── Dividend ───────────────────────────────────────────────────────────

    def _import_dividend(self, row: dict, desc: str, symbol: str | None,
                         date: datetime.datetime, credit: float | None) -> None:
        if credit is None:
            self._result.errors.append(
                f"Dividend on {date.date()} has no credit amount — skipped."
            )
            self._result.skipped += 1
            return

        if not symbol:
            self._result.errors.append(
                f"Dividend on {date.date()} has no symbol — skipped."
            )
            self._result.skipped += 1
            return

        m = _DIV_RE.match(desc)
        qty = float(m.group(1)) if m else None
        name = " ".join(m.group(2).split()) if m else None  # collapse whitespace

        # Ensure instrument exists (gilts / bonds may not be in the main holdings)
        self._ensure_instrument(symbol, name)

        gross_per_share = (credit / qty) if (qty and qty > 0) else None
        reference = f"div_{self.platform_id}_{symbol}_{date.strftime('%Y-%m-%d')}"

        stmt = (
            pg_insert(DividendPayment)
            .values(
                reference=reference,
                ticker=symbol,
                platform_id=self.platform_id,
                account=self.platform.account_type,
                user_id=self.user_id,
                quantity=qty,
                amount=credit,
                gross_amount_per_share=gross_per_share,
                paid_on=date,
                type="DIVIDEND",
                synced_at=datetime.datetime.utcnow(),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "reference"],
                set_={
                    "platform_id": self.platform_id,
                    "account": self.platform.account_type,
                    "amount": credit,
                    "quantity": qty,
                    "gross_amount_per_share": gross_per_share,
                    "paid_on": date,
                    "synced_at": datetime.datetime.utcnow(),
                },
            )
        )
        self._db.execute(stmt)
        self._result.dividends_upserted += 1

    # ── Transaction ────────────────────────────────────────────────────────

    def _import_transaction(self, row_type: str, desc: str, symbol: str | None,
                            ref: str | None, date: datetime.datetime,
                            debit: float | None, credit: float | None) -> None:
        amount = credit if credit is not None else (-(debit) if debit is not None else None)
        if amount is None:
            self._result.skipped += 1
            return

        # Use provided reference for trades; synthesise for everything else
        if ref:
            tx_id = ref
        else:
            tx_id = _synth_id(self.platform_id, row_type, date, abs(amount))

        type_map = {
            "interest":       "INTEREST",
            "bond_redemption": "BOND_REDEMPTION",
            "trade_buy":      "TRADE_BUY",
            "trade_sell":     "TRADE_SELL",
            "transfer_in":    "TRANSFER_IN",
            "transfer_out":   "TRANSFER_OUT",
            "cashback":       "CASHBACK",
            "deposit":        "DEPOSIT",
            "other":          "OTHER",
        }
        tx_type = type_map.get(row_type, "OTHER")

        # For trades, ensure the instrument record exists
        if row_type in ("trade_buy", "trade_sell") and symbol:
            self._ensure_instrument(symbol, None)

        notes = desc if desc else None

        stmt = (
            pg_insert(Transaction)
            .values(
                id=tx_id,
                user_id=self.user_id,
                platform_id=self.platform_id,
                type=tx_type,
                amount=amount,
                date_time=date,
                reference=tx_id,
                notes=notes,
                synced_at=datetime.datetime.utcnow(),
            )
            .on_conflict_do_update(
                constraint="uq_transactions_user_t212",
                set_={
                    "platform_id": self.platform_id,
                    "type": tx_type,
                    "amount": amount,
                    "date_time": date,
                    "notes": notes,
                    "synced_at": datetime.datetime.utcnow(),
                },
            )
        )
        self._db.execute(stmt)
        self._result.transactions_upserted += 1

    # ── Instruments ────────────────────────────────────────────────────────

    def _ensure_instrument(self, ticker: str, name: str | None) -> None:
        existing = self._db.get(Instrument, ticker)
        if existing:
            return
        inst = Instrument(
            ticker=ticker,
            name=name or ticker,
            currency_code="GBP",
            instrument_type="STOCK",
        )
        self._db.add(inst)
        self._db.flush()
        self._result.instruments_created += 1
