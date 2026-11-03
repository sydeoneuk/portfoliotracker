"""Read statement costs into existing account positions."""
import csv
from datetime import timedelta
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.importers.ii_csv import IiCsvImporter, _clean
from sqlalchemy import func

from app.models import Order, Position


@dataclass
class PurchaseCostResult:
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self):
        return f"{self.updated} purchase costs to update, {self.skipped} rows skipped."


def import_purchase_costs(db, account, batch):
    if account.platform_type not in {'interactive_investor', 't212_csv'}:
        raise ValueError('Purchase cost uploads are for Interactive Investor and Trading 212 CSV accounts.')
    reader = csv.DictReader(io.StringIO(batch.raw_content.decode('utf-8-sig')))
    headers = {name: _clean(name).lower().replace(' £', '').replace('£', '').strip()
               for name in reader.fieldnames or []}
    fields = set(headers.values())
    if len(headers) != len(reader.fieldnames or []) or len(fields) != len(headers):
        raise ValueError('Duplicate CSV columns')
    symbol_fields = fields & {'symbol', 'ticker', 'epic', 'code'}
    if len(symbol_fields) != 1 or 'qty' not in fields:
        raise ValueError('CSV requires a single Symbol or Ticker column and Qty.')
    ii_export = account.platform_type == 'interactive_investor' and 'price' in fields and 'currency' not in fields
    if not ii_export and not {'book cost', 'currency'} <= fields:
        raise ValueError('CSV requires Book Cost and Currency columns.')
    importer = IiCsvImporter(db, account, batch.user_id, managed=True) if ii_export else None
    result = PurchaseCostResult()
    seen = set()
    for number, raw in enumerate(reader, start=2):
        try:
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError('Unexpected number of CSV cells')
            row = {headers[key]: value.strip() for key, value in raw.items()}
            if not any(row.values()):
                continue
            if ii_export:
                parsed = importer._parse_row(raw, headers)
                if parsed is None:
                    continue
                ticker, quantity = parsed['ticker'], Decimal(str(parsed['quantity']))
                amount = parsed['basis']['cost_basis_amount']
                currency = parsed['basis']['cost_basis_currency']
                if amount is None:
                    result.skipped += 1
                    continue
                amount = Decimal(str(amount))
            else:
                ticker = row[next(iter(symbol_fields))]
                quantity = Decimal(row['qty'])
                if not row['book cost'] and not row['currency']:
                    result.skipped += 1
                    continue
                book_cost = row['book cost']
                if ',' in book_cost:
                    if not re.fullmatch(r'[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?', book_cost):
                        raise ValueError('Invalid thousands separators in Book Cost')
                    book_cost = book_cost.replace(',', '')
                amount = Decimal(book_cost)
                currency = row['currency']
                currency = 'GBX' if currency == 'GBp' else currency.upper()
            if not quantity.is_finite() or quantity <= 0 or not amount.is_finite() or amount <= 0:
                raise ValueError('Quantity and book cost must be finite and positive')
            if amount >= Decimal('1e18') or amount.quantize(Decimal('0.0000000001')) != amount:
                raise ValueError('Book cost is outside the supported range')
            if not currency or not re.fullmatch(r'[A-Z]{3}', currency):
                raise ValueError('Enter an explicit three-letter cost currency, such as GBP or GBX')
            if ticker in seen:
                raise ValueError(f'Duplicate holding {ticker}')
            seen.add(ticker)
            position = db.get(Position, (ticker, account.id, batch.user_id))
            if position is None or position.quantity is None:
                raise ValueError(f'{ticker} does not match a holding in this account')
            if quantity != Decimal(str(position.quantity)):
                raise ValueError(f'{ticker} statement quantity differs from the current holding')
            if position.cost_basis_amount == amount and position.cost_basis_currency == currency and position.cost_basis_status == 'reported':
                result.skipped += 1
                continue
            if (not batch.options.get('replace_purchase_costs')
                    and position.cost_basis_status in {'reported', 'estimated'}
                    and position.cost_basis_amount is not None and position.cost_basis_currency):
                result.skipped += 1
                continue
            later_trade = db.query(Order.pk).filter(
                Order.user_id == batch.user_id, Order.platform_id == account.id,
                Order.ticker == ticker, Order.status == 'FILLED',
                func.coalesce(Order.filled_at, Order.created_at) >= batch.effective_date + timedelta(days=1),
            ).first()
            if later_trade:
                raise ValueError(f'{ticker} has a trade after the statement date')
            position.cost_basis_amount = amount
            position.cost_basis_currency = currency
            position.cost_basis_status = 'reported'
            position.cost_basis_source = 'broker'
            position.cost_basis_reason = f'Purchase cost statement dated {batch.effective_date}'
            result.updated += 1
        except InvalidOperation as exc:
            raise ValueError(f'Row {number}: Enter a numeric quantity and book cost') from exc
        except ValueError as exc:
            raise ValueError(f'Row {number}: {exc or "Invalid numeric value"}') from exc
    if not seen:
        raise ValueError('No rows with usable purchase costs were found.')
    return result
