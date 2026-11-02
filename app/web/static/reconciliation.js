(() => {
  'use strict';
  const root = document.getElementById('listing-reconciliation');
  if (!root) return;
  const el = id => document.getElementById(id);
  const state = {offset: 0, limit: 10, items: [], request: 0};
  const node = (tag, text, css) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (css) result.className = css;
    return result;
  };
  const status = (message, error = false) => {
    el('recon-status').textContent = message;
    el('recon-status').className = error ? 'small text-danger mb-2' : 'small text-muted mb-2';
  };
  async function api(path = '', body) {
    const response = await fetch('/settings/reconciliation' + path, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request could not be completed.');
    return data;
  }
  function button(label, action) {
    const control = node('button', label, 'btn btn-sm btn-outline-secondary');
    control.type = 'button';
    control.addEventListener('click', async () => {
      control.disabled = true;
      control.textContent = 'Working…';
      try { await action(); } catch (error) { status(error.message, true); }
      finally { control.disabled = false; control.textContent = label; }
    });
    return control;
  }
  function renderProposals(container, proposals, symbolInput) {
    container.replaceChildren();
    for (const proposal of proposals) {
      const candidate = proposal.candidate;
      const box = node('div', undefined, 'border rounded p-2 mt-2');
      box.append(node('strong', (candidate.symbol || 'No candidate') + ' — ' + proposal.status.replaceAll('_', ' ')));
      if (candidate.name) box.append(node('div', candidate.name));
      box.append(node('div', 'ISIN: ' + (candidate.isin || 'unverified') + ' · Venue: ' + (candidate.exchange || 'unverified') + ' · Currency: ' + (candidate.currency || 'unknown'), 'small text-muted'));
      if (candidate.price !== undefined) {
        box.append(node('div', 'Quote: ' + candidate.price + ' ' + candidate.currency + ' · ' + (candidate.observed_at || '').slice(0, 10), 'small'));
      }
      if (candidate.normalized_price !== undefined) {
        box.append(node('div', 'Price used per share: ' + candidate.normalized_price + ' ' + candidate.display_units + ' (conversion rate ' + candidate.conversion_rate + ').', 'small'));
      }
      const evidence = node('ul', undefined, 'small mb-2');
      for (const reason of candidate.evidence || []) evidence.append(node('li', reason));
      box.append(evidence);
      if (candidate.identity_evidence) {
        const details = node('details');
        details.append(node('summary', 'Identity evidence'));
        details.append(node('pre', JSON.stringify(candidate.identity_evidence, null, 2), 'small text-wrap'));
        box.append(details);
      }
      if (candidate.metadata_correction) {
        const correction = candidate.metadata_correction;
        box.append(node('div', 'Shared catalogue correction — statement: ' + correction.statement_reference, 'fw-semibold mt-2'));
        for (const field of ['isin', 'currency_code', 'mic_code']) {
          box.append(node('div', field + ': ' + (correction.before[field] || 'missing') + ' → ' + (correction.after[field] || 'missing'), 'small'));
        }
        box.append(node('div', 'Applies the metadata and verified price mapping together. Original quantities, transactions and costs are retained.', 'small'));
      }
      if (proposal.can_apply) {
        box.append(button(candidate.metadata_correction ? 'Apply verified metadata and mapping' : 'Apply verified correction', async () => {
          const data = await api('/apply', {proposal_id: proposal.id});
          await load(); await history(); status(data.message + '. Previous values are retained in correction history.');
        }));
      } else if (proposal.status === 'ambiguous' && candidate.verified && candidate.symbol) {
        box.append(button('Choose this symbol for review', async () => {
          symbolInput.value = candidate.symbol; symbolInput.focus();
          status('Review the venue and units, then verify this symbol. No correction has been applied.');
        }));
      } else if (proposal.status === 'needs_review') {
        box.append(node('div', 'Cannot apply this symbol: verification did not pass. Resolve the reasons above before verifying again. Choosing the same symbol again will not bypass these checks.', 'small text-danger'));
      } else if (proposal.status === 'expired' || proposal.status === 'superseded') {
        box.append(node('div', 'This review is no longer current. Verify the entered symbol again to obtain a new proposal.', 'small text-muted'));
      }
      container.append(box);
    }
  }
  function renderItem(item) {
    const card = node('details', undefined, 'border rounded p-3 mb-2');
    const title = node('summary', item.ticker + ' — ' + (item.name || item.ticker), 'fw-semibold');
    card.append(title);
    card.append(node('div', 'ISIN: ' + (item.isin || 'missing') + ' · Recorded units: ' + (item.currency_code || 'missing') + ' · Quantity: ' + item.quantity, 'small mt-2'));
    card.append(node('div', 'Current price mapping: ' + (item.mapping ? item.mapping.provider_symbol : item.yf_ticker || 'none') + ' · ' + (item.quote_available ? 'Quote available' : 'No usable quote'), 'small text-muted'));
    if (!item.costs_available) card.append(node('div', 'Cost needs an API refresh or source-statement reconciliation.', 'small text-muted'));
    const actions = node('div', undefined, 'd-flex flex-wrap gap-2 my-2');
    const proposals = node('div');
    const input = node('input', undefined, 'form-control form-control-sm');
    input.placeholder = 'Yahoo symbol, e.g. SHEL.L'; input.maxLength = 40;
    input.setAttribute('aria-label', 'Provider symbol for ' + item.ticker);
    const label = node('label', undefined, 'small d-flex gap-2 align-items-start mt-2');
    const conversion = node('input'); conversion.type = 'checkbox';
    label.append(conversion, node('span', 'After reviewing the listing, allow conversion from its verified quote currency into the recorded holding units. Original costs and currency fields will not be rewritten.'));
    async function verify(symbol) {
      status('Checking ' + item.ticker + ' against market-data identity sources…');
      const data = await api('/propose', {ticker: item.ticker, symbol: symbol || null, allow_currency_conversion: Boolean(symbol && conversion.checked)});
      renderProposals(proposals, data.proposals, input);
      card.open = true;
      if (data.proposals.some(proposal => proposal.can_apply)) {
        status('Verification passed. Review the evidence below, then choose Apply verified correction.');
      } else if (data.proposals.some(proposal => proposal.status === 'ambiguous')) {
        status('More than one listing passed verification. Choose a symbol for review, then verify that selection.');
      } else {
        status('Verification failed. Apply is unavailable; resolve the reasons shown below before trying again.', true);
      }
    }
    actions.append(button('Find matches', () => verify(null)));
    if (item.mapping) actions.append(button('Refresh quote', async () => {
      const data = await api('/refresh', {mapping_id: item.mapping.id}); await load(); status(data.message);
    }));
    if (item.undo_audit_id) actions.append(button('Undo latest correction', async () => {
      const data = await api('/undo', {audit_id: item.undo_audit_id}); await load(); await history(); status(data.message);
    }));
    card.append(actions, input, label, button('Verify entered symbol', async () => {
      if (!input.value.trim()) throw new Error('Enter a provider symbol first.');
      await verify(input.value.trim());
    }), proposals);
    renderProposals(proposals, item.proposals, input);
    if (root.dataset?.metadataAdmin === 'true') {
      const metadata = node('details', undefined, 'mt-3 border-top pt-2');
      metadata.append(node('summary', 'Admin: reconcile statement identity or missing currency'));
      const field = (title, value, placeholder) => {
        const label = node('label', title, 'd-block small mt-2');
        const control = node('input', undefined, 'form-control form-control-sm');
        control.value = value || ''; control.placeholder = placeholder;
        label.append(control); metadata.append(label); return control;
      };
      const isin = field('Statement ISIN', item.isin, '12-character ISIN'); isin.maxLength = 12;
      const currency = field('Statement price units', item.currency_code, 'GBP, GBX (pence), USD, EUR…'); currency.maxLength = 3;
      const reference = field('Statement reference and date', '', 'Source statement identifying this holding'); reference.maxLength = 240;
      metadata.append(node('p', 'The provider symbol above must match the statement ISIN. This updates catalogue metadata shared by your accounts; it cannot reinterpret existing costs.', 'small text-muted mt-2'));
      metadata.append(button('Verify statement correction', async () => {
        status('Verifying statement metadata against the provider…');
        const data = await fetchMetadata({ticker: item.ticker, symbol: input.value.trim(),
          isin: isin.value.trim(), currency: currency.value.trim(), statement_reference: reference.value.trim()});
        renderProposals(proposals, data.proposals, input); card.open = true;
        status(data.proposals.some(p => p.can_apply) ? 'Metadata verified. Review the before/after values and apply below.' :
          'Statement verification failed. Review the provider evidence below.', !data.proposals.some(p => p.can_apply));
      }));
      card.append(metadata);
    }
    return card;
  }
  async function fetchMetadata(body) {
    const response = await fetch('/admin/reconciliation/metadata/propose', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Complete the statement fields and provider symbol.');
    return data;
  }
  async function load() {
    const serial = ++state.request;
    const query = new URLSearchParams({search: el('recon-query').value, missing_only: String(el('recon-missing').checked), offset: state.offset, limit: state.limit});
    const data = await api('?' + query);
    if (serial !== state.request) return;
    state.items = data.items;
    el('recon-results').replaceChildren(...data.items.map(renderItem));
    if (!data.items.length) el('recon-results').append(node('p', 'No holdings match this view. Uncheck “missing quotes only” to see existing corrections.', 'text-muted'));
    el('recon-page').textContent = data.total ? (data.offset + 1) + '–' + (data.offset + data.items.length) + ' of ' + data.total : '0 holdings';
    el('recon-prev').disabled = state.offset === 0;
    el('recon-next').disabled = state.offset + state.limit >= data.total;
  }
  async function history() {
    const data = await api('/history');
    const rows = data.items.map(item => {
      const row = node('div', undefined, 'border-bottom py-2');
      row.append(node('div', item.ticker + ' · ' + item.action + ' · ' + item.created_at.slice(0, 19).replace('T', ' ') + ' UTC'));
      row.append(node('div', (item.before ? item.before.provider_symbol : 'Shared catalogue') + ' → ' + (item.after ? item.after.provider_symbol : 'Shared catalogue'), 'text-muted'));
      if (item.after?.metadata_correction) {
        const correction = item.after.metadata_correction;
        row.append(node('div', 'Metadata: ' + JSON.stringify(correction.before) + ' → ' + JSON.stringify(correction.after) + ' · Statement: ' + correction.statement_reference, 'small'));
      }
      if (item.can_undo) row.append(button('Undo this correction', async () => {
        const result = await api('/undo', {audit_id: item.id}); await load(); await history(); status(result.message);
      }));
      return row;
    });
    el('recon-history').replaceChildren(...rows);
    if (!rows.length) el('recon-history').append(node('p', 'No corrections applied yet.', 'text-muted'));
  }
  el('recon-search').addEventListener('submit', event => {
    event.preventDefault(); state.offset = 0; load().catch(error => status(error.message, true));
  });
  el('recon-missing').addEventListener('change', () => {state.offset = 0; load().catch(error => status(error.message, true));});
  el('recon-prev').addEventListener('click', () => {state.offset = Math.max(0, state.offset - state.limit); load().catch(error => status(error.message, true));});
  el('recon-next').addEventListener('click', () => {state.offset += state.limit; load().catch(error => status(error.message, true));});
  el('recon-refresh').addEventListener('click', async event => {
    const mappings = state.items.filter(item => item.mapping);
    if (!mappings.length) return status('No verified mappings on this page. Uncheck the missing-quotes filter to see applied corrections.');
    event.target.disabled = true;
    let refreshed = 0, failed = 0;
    for (const item of mappings) {
      status('Refreshing ' + item.ticker + '…');
      try {await api('/refresh', {mapping_id: item.mapping.id}); refreshed++;} catch (_) {failed++;}
    }
    event.target.disabled = false;
    await load(); status(refreshed + ' refreshed; ' + failed + ' could not be verified. Previous quotes are retained on failure.', failed > 0);
  });
  Promise.all([load(), history()]).catch(error => status(error.message, true));
})();
