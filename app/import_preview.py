"""Customer-facing explanations of existing import policies; no database writes."""

def import_effect(batch):
    """Explain existing import rules without changing or reinterpreting observations."""
    if batch.source == 't212_identity_reconciliation':
        explanation = 'Assigns an existing cash transaction to its verified broker account. Amounts, dates and holdings stay unchanged.'
    elif batch.source == 'purchase_costs':
        if batch.options.get('replace_purchase_costs'):
            explanation = 'Fills missing costs and replaces existing costs for matching rows in this statement. Review each old and new cost before applying.'
        else:
            explanation = 'Fills missing purchase costs for matching holdings in the selected account. Existing usable costs and quantities are preserved.'
    elif batch.source == 'pie_snapshot':
        explanation = 'Updates this pie’s allocation only. Account holdings and purchase costs stay unchanged.'
    elif batch.source == 'ii_holdings':
        explanation = 'Uses this statement for account quantities and reported purchase costs on its effective date. Holdings absent from the statement are removed from this account.'
    elif batch.history_complete:
        explanation = 'Rebuilds holdings and calculated purchase costs from the complete transaction history you confirmed.'
    elif batch.source == 't212_api':
        explanation = 'Uses broker-reported account holdings.'
    else:
        explanation = 'Adds transaction activity. Current holdings and their purchase costs stay unchanged.'
    result = {'explanation': explanation}
    # Pie allocations have no independent book-cost contract. Event-only previews
    # must not imply that existing account positions came from the uploaded file.
    if batch.source == 'ii_holdings' or (batch.source == 't212_history' and batch.history_complete):
        rows = batch.review.get('proposed')
        if rows is not None and batch.status != 'rejected':
            held = [row for row in rows if float(row.get('quantity') or 0) > 0]
            result['holdings_count'] = len(held)
            result['costs_unavailable'] = sum(
                row.get('cost_basis_status') not in {'reported', 'estimated'}
                or row.get('cost_basis_amount') is None or not row.get('cost_basis_currency')
                for row in held)
    return result
