"""Chronological moving-average portfolio cost, not a tax-lot calculation."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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


def purchase_costs(db, user_id, positions):
    """Read explicit costs, recovering missing fields from the current observation."""
    from app.models import ImportAuthority, ImportHoldingObservation, IngestionBatch

    costs = {}
    missing = {}
    for position in positions:
        key = (position.platform_id, position.ticker)
        if (position.cost_basis_status in {"reported", "estimated"}
                and position.cost_basis_amount is not None and position.cost_basis_currency):
            costs[key] = (Decimal(str(position.cost_basis_amount)), position.cost_basis_currency,
                          position.cost_basis_status)
        elif not position.cost_basis_reason:
            missing[key] = position
    if not missing:
        return costs

    observations = db.query(IngestionBatch.platform_id, ImportHoldingObservation.holdings).join(
        ImportHoldingObservation, ImportHoldingObservation.batch_id == IngestionBatch.id,
    ).join(ImportAuthority, ImportAuthority.batch_id == IngestionBatch.id).filter(
        ImportAuthority.user_id == user_id, ImportAuthority.scope == 'account',
        ImportAuthority.platform_id == IngestionBatch.platform_id,
        IngestionBatch.user_id == user_id, IngestionBatch.status == 'applied',
        ImportHoldingObservation.user_id == user_id,
        ImportHoldingObservation.scope == 'account', ImportHoldingObservation.phase == 'applied',
        ImportHoldingObservation.complete.is_(True),
        IngestionBatch.platform_id.in_({key[0] for key in missing}),
    )
    recovered = {}
    for platform_id, holdings in observations:
        for row in holdings:
            key = (platform_id, row.get('ticker'))
            position = missing.get(key)
            if position is None or position.last_synced_at is None:
                continue
            observed_at = row.get('last_synced_at')
            if observed_at not in {position.last_synced_at.isoformat(), str(position.last_synced_at)}:
                continue
            if row.get('quantity') != position.quantity:
                continue
            status = row.get('cost_basis_status')
            currency = row.get('cost_basis_currency')
            amount = row.get('cost_basis_amount')
            if status in {'reported', 'estimated'} and currency and amount is not None:
                try:
                    amount = Decimal(str(amount))
                except InvalidOperation:
                    continue
                if amount.is_finite() and amount >= 0:
                    recovered.setdefault(key, set()).add((amount, currency, status))
    for key, candidates in recovered.items():
        if len(candidates) == 1:
            costs[key] = candidates.pop()
    return costs


def decorate_cost_basis(db, user_id, rows, platform_ids=None, selected_pies=(), outside=False):
    """Allocate each account's cost by its actual selected holding quantity."""
    from app.models import Position, Pie, PieHolding
    positions = db.query(Position).filter_by(user_id=user_id).all()
    costs = purchase_costs(db, user_id, positions)
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
        amount, currency, status = costs.get((position.platform_id, position.ticker), (None, None, 'unknown'))
        component = dict(amount=float(amount * desired / quantity) if amount is not None else None,
                         currency=currency, status=status)
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
