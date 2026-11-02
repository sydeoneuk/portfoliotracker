"""Chronological moving-average portfolio cost, not a tax-lot calculation."""
from dataclasses import dataclass
from decimal import Decimal
from itertools import groupby


@dataclass(frozen=True)
class CostBasis:
    quantity: Decimal
    amount: Decimal | None
    currency: str | None
    reason: str | None
    quantity_reconciled: bool


def moving_average_cost(trades) -> CostBasis:
    quantity, amount, currency = Decimal(0), Decimal(0), None
    reason, reconciled = None, True
    trades = sorted(trades, key=lambda t: (t.filled_at is None, t.filled_at, t.pk))
    for timestamp, grouped in groupby(trades, key=lambda t: t.filled_at):
        group = list(grouped)
        ambiguous = timestamp is None or len({t.side for t in group}) > 1
        for trade in group:
            if trade.filled_quantity is None:
                reconciled, amount, reason = False, None, "Filled quantity unavailable"
                continue
            qty = Decimal(str(trade.filled_quantity))
            if not qty.is_finite() or qty <= 0 or trade.side not in {"BUY", "SELL"}:
                raise ValueError("Invalid trade quantity or side")
            if ambiguous:
                amount, reason = None, "Missing or ambiguous execution chronology"
            if trade.side == "BUY":
                # A reported settlement total is already the cash paid, including
                # charges. Do not add separately listed fees to it a second time.
                cash = trade.settlement_amount
                ccy = trade.settlement_currency
                if cash is None or not ccy:
                    amount, reason = None, "Settlement amount or currency unavailable"
                elif amount is not None:
                    cash = abs(Decimal(str(cash)))
                    if not cash.is_finite():
                        raise ValueError("Invalid settlement amount")
                    if currency is not None and currency != ccy:
                        amount, reason = None, "Acquisition currencies differ; historical conversion required"
                    else:
                        amount += cash
                        currency = ccy
                quantity += qty
            else:
                if qty > quantity:
                    reconciled = False
                    amount, reason = None, "Disposal exceeds known purchases; opening history required"
                elif amount is not None and quantity:
                    amount *= (quantity - qty) / quantity
                quantity -= qty
                if quantity == 0 and reconciled and not ambiguous:
                    amount, currency, reason = Decimal(0), None, None
    if not reconciled:
        amount, reason = None, "Opening history required; quantities cannot be reconciled"
    return CostBasis(quantity, amount, currency, reason, reconciled)


def broker_cost_values(quantity, average_price, currency):
    amount = None
    if quantity is not None and average_price is not None and currency:
        quantity, price = Decimal(str(quantity)), Decimal(str(average_price))
        if quantity.is_finite() and price.is_finite() and quantity >= 0 and price >= 0:
            amount = quantity * price
    return dict(cost_basis_amount=amount, cost_basis_currency=currency,
                cost_basis_source="broker", cost_basis_status="reported" if amount is not None else "unknown",
                cost_basis_reason=None if amount is not None else "Reported cost or currency unavailable")


def decorate_cost_basis(db, user_id, rows, platform_ids=None, selected_pies=(), outside=False):
    """Allocate each account's cost by its actual selected holding quantity."""
    from app.models import Position, Pie, PieHolding
    positions = db.query(Position).filter_by(user_id=user_id).all()
    allocations = db.query(PieHolding, Pie.platform_id).join(Pie, Pie.pk == PieHolding.pie_id).filter(
        Pie.user_id == user_id, PieHolding.user_id == user_id).all() if selected_pies or outside else []
    grouped = {}
    for position in positions:
        if platform_ids is not None and position.platform_id not in platform_ids:
            continue
        quantity = Decimal(str(position.quantity or 0))
        if quantity <= 0:
            continue
        desired = quantity
        if selected_pies or outside:
            all_qty, chosen = Decimal(0), Decimal(0)
            for holding, platform_id in allocations:
                if platform_id == position.platform_id and holding.ticker == position.ticker:
                    held = Decimal(str(holding.owned_quantity or 0))
                    all_qty += held
                    if holding.pie_id in selected_pies:
                        chosen += held
            desired = chosen + (max(quantity - all_qty, Decimal(0)) if outside else Decimal(0))
        if desired <= 0:
            continue
        known = position.cost_basis_status in {"reported", "estimated"} and position.cost_basis_amount is not None and bool(position.cost_basis_currency)
        component = dict(amount=float(position.cost_basis_amount * desired / quantity) if known else None,
                         currency=position.cost_basis_currency, status=position.cost_basis_status or "legacy_unknown")
        grouped.setdefault(position.ticker, []).append(component)
    for row in rows:
        components = grouped.get(row["ticker"], [])
        row["cost_components"] = components
        row["cost_unknown"] = not components or any(c["amount"] is None for c in components)
        row["cost_estimated"] = any(c["status"] == "estimated" for c in components)
        # Old SQL fields have ambiguous legacy units. All consumers use the
        # explicitly denominated components instead.
        row["cost"] = row["average_price"] = row["ppl"] = row["result_coef"] = None
    return rows
