(() => {
  'use strict';
  const node = (tag, text) => {const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el;};
  const panels = new Map();
  const purchaseCost = holding => {
    if (!holding) return '—';
    if (!['reported', 'estimated'].includes(holding.cost_basis_status)
        || holding.cost_basis_amount == null || !holding.cost_basis_currency) return 'Unavailable';
    return `${holding.cost_basis_amount} ${holding.cost_basis_currency}${holding.cost_basis_status === 'estimated' ? ' (calculated)' : ' (reported)'}`;
  };
  function attach(input) {
    const panel = node('div'); panel.className = 'small border rounded p-2 mt-2';
    const field = (label, type) => {
      const wrapper = node('label', label); wrapper.className = 'd-block mt-1';
      const control = node('input'); control.type = type; control.className = type === 'checkbox' ? 'form-check-input ms-2' : 'form-control form-control-sm';
      wrapper.append(control); panel.append(wrapper); return [control, wrapper];
    };
    const [effective, effectiveLabel] = field('Snapshot effective date from statement', 'date');
    const [complete, completeLabel] = field('Use complete transaction history as the holdings source (includes opening balances and all movements)', 'checkbox');
    const [start, startLabel] = field('Complete history starts', 'date');
    const [end, endLabel] = field('Complete history ends / holdings effective date', 'date');
    const advanced = node('details'); advanced.append(node('summary', 'Advanced: rebuild holdings from complete history'));
    advanced.append(completeLabel, startLabel, endLabel); panel.append(advanced);
    const note = node('p', 'Upload a file to preview changes. Transaction files add activity by default; holdings statements update account holdings after review.');
    note.className = 'text-muted mb-0 mt-2'; panel.append(note);
    input.after(panel);
    panels.set(input, {effective, complete, start, end});
    const configure = () => {
      let type = '';
      if (input.id === 'csv-import-file') type = document.getElementById('csv-import-platform-type').value;
      if (input.id === 'add-csv-file') type = document.getElementById('add-account-step2-platform-type').value;
      const snapshot = input.id === 'pie-csv-file' || type === 'interactive_investor';
      const tradeHistory = ['csv-import-file', 'add-csv-file'].includes(input.id) && !snapshot && type !== 't212_api';
      effectiveLabel.hidden = !snapshot;
      completeLabel.hidden = !tradeHistory;
      advanced.hidden = !tradeHistory;
      startLabel.hidden = endLabel.hidden = !tradeHistory || !complete.checked;
    };
    input.addEventListener('change', () => {complete.checked = false; effective.value = start.value = end.value = ''; configure();});
    complete.addEventListener('change', configure); configure();
  }
  function options(input, form, kind) {
    const fields = panels.get(input);
    if (kind === 'snapshot') {
      if (!fields.effective.value) throw new Error('Enter the snapshot effective date shown on the statement.');
      form.append('effective_date', fields.effective.value);
    }
    if (kind === 't212_history' && fields.complete.checked) {
      if (!fields.start.value || !fields.end.value) throw new Error('Enter complete history coverage dates.');
      form.append('history_complete', 'true');
      form.append('coverage_start', fields.start.value); form.append('coverage_end', fields.end.value);
    }
  }
  function render(container, data) {
    container.replaceChildren(); container.style.display = '';
    container.className = 'alert mb-0 ' + (data.status === 'rejected' ? 'alert-danger' : 'alert-info');
    container.append(node('strong', data.summary));
    container.append(node('p', data.import_effect?.explanation || 'Review the proposed changes before importing.'));
    if (data.effective_date) container.append(node('p', 'Statement date: ' + data.effective_date));
    if (data.import_effect?.holdings_count !== undefined) {
      container.append(node('p', `${data.import_effect.holdings_count} holdings; purchase cost unavailable for ${data.import_effect.costs_unavailable}.`));
      if (data.import_effect.costs_unavailable) container.append(node('p', 'You can import these holdings. Returns will be incomplete until purchase costs are available.'));
    }
    const technical = node('details'); technical.append(node('summary', 'Technical import details'));
    technical.append(node('p', `Import #${data.batch_id} · ${data.status} · ${data.source} · Parser: ${data.parser_version}`));
    technical.append(node('p', 'Checksum: ' + data.checksum));
    if (data.identity_adoption) technical.append(node('pre', JSON.stringify(data.identity_adoption, null, 2)));
    if (data.exact_file_seen_in_batch) container.append(node('p', 'This exact file was already applied in import #' + data.exact_file_seen_in_batch + '. Overlapping files still require source-reference reconciliation.'));
    if (data.current_authority) technical.append(node('p', 'Current holdings source: ' + data.current_authority.source + ' · Effective date: ' + (data.current_authority.effective_date || 'unknown')));
    for (const message of [...(data.errors || []), ...(data.warnings || [])]) container.append(node('p', message));
    if (data.before && data.proposed) {
      const details = node('details'); details.append(node('summary', 'Review holding changes'));
      const table = node('table'); table.className = 'table table-sm small';
      const head = node('tr'); for (const value of ['Holding', 'Current quantity', 'New quantity', 'Current purchase cost', 'New purchase cost']) head.append(node('th', value)); table.append(head);
      const before = new Map(data.before.map(row => [row.ticker, row]));
      const proposed = new Map(data.proposed.map(row => [row.ticker, row]));
      let count = 0;
      for (const ticker of new Set([...before.keys(), ...proposed.keys()])) {
        const old = before.get(ticker), next = proposed.get(ticker);
        if (JSON.stringify(old) === JSON.stringify(next)) continue;
        count++;
        const row = node('tr'); row.append(node('td', ticker));
        row.append(node('td', old?.quantity ?? 'Absent'), node('td', next?.quantity ?? 'Removed'));
        row.append(node('td', purchaseCost(old)), node('td', purchaseCost(next)));
        table.append(row);
      }
      if (count) {details.append(table); details.open = true;} else details.append(node('p', 'No holding changes. Imported events may still be added.'));
      container.append(details);
      technical.append(node('pre', JSON.stringify({before: data.before, proposed: data.proposed}, null, 2)));
    }
    container.append(technical);
    if (data.status === 'applied' && ['t212_history', 'ii_holdings', 'ii_transactions', 'pie_snapshot'].includes(data.source)) {
      const match = node('button', 'Match investments'); match.type = 'button';
      match.className = 'btn btn-sm btn-outline-primary d-block mt-2';
      const progress = node('div'); container.append(match, progress);
      const runMatching = async () => {
        match.disabled = true; progress.replaceChildren(node('p', 'Checking investment matches…'));
        try {
          const response = await fetch(`/imports/${data.batch_id}/matching`);
          const work = await response.json();
          if (!response.ok) throw new Error(work.detail || 'Unable to load investments to match.');
          progress.replaceChildren();
          const status = node('p'); progress.append(status);
          let done = 0, matched = 0, attention = 0;
          for (const ticker of work.tickers) {
            status.textContent = `Checking ${++done} of ${work.tickers.length}: ${ticker}`;
            const response = await fetch(`/imports/${data.batch_id}/matching`, {method: 'POST',
              headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ticker})});
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || 'Matching interrupted. Completed matches are retained.');
            if (['matched', 'reused'].includes(result.status)) matched++;
            else {attention++; progress.append(node('p', `${ticker}: Needs attention — ${result.reason}`));}
            if (result.pricing?.status === 'unavailable') progress.append(node('p', `${ticker}: Price unavailable — ${result.pricing.reason}`));
          }
          status.textContent = `${matched} investments matched; ${attention} need attention. Your import is complete.`;
          progress.append(node('p', 'Matches and verified quotes are saved for this account. Purchase costs and transaction history are unchanged.'));
          const review = node('a', 'Review holdings needing attention'); review.href = '/settings#holding-attention'; progress.append(review);
        } catch (error) {progress.append(node('p', error.message));}
        finally {match.disabled = false;}
      };
      match.addEventListener('click', runMatching);
      // Applying a new upload starts matching; opening import history offers a retry.
      if (data.start_matching) runMatching();
    }
    if (data.can_apply) {
      const label = node('label', ' I have reviewed the statement date and proposed changes. ');
      const checkbox = node('input'); checkbox.type = 'checkbox'; label.prepend(checkbox); container.append(label);
      const apply = node('button', 'Apply reviewed import'); apply.type = 'button'; apply.className = 'btn btn-sm btn-primary d-block mt-2'; apply.disabled = true;
      checkbox.addEventListener('change', () => {apply.disabled = !checkbox.checked;});
      apply.addEventListener('click', async () => {
        apply.disabled = true;
        try {
          const response = await fetch('/imports/apply', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({batch_id: data.batch_id, reviewed: checkbox.checked})});
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || 'Import could not be applied.');
          render(container, {...result, start_matching: true}); if (typeof window.loadAccounts === 'function') window.loadAccounts();
        } catch (error) {container.append(node('p', error.message)); apply.disabled = false;}
      });
      container.append(apply);
    }
  }
  window.ImportReview = {options, render};
  for (const id of ['csv-import-file', 'add-csv-file', 'pie-csv-file', 'ii-tx-csv-file']) {
    const input = document.getElementById(id); if (input) attach(input);
  }
  const history = document.getElementById('import-history-results');
  const load = async () => {
    const response = await fetch('/imports'); const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Unable to load imports');
    history.replaceChildren();
    for (const item of data.items) {
      const button = node('button', `#${item.batch_id} · ${item.filename} · ${item.status} · ${item.effective_date || 'event-only'}`);
      button.type = 'button'; button.className = 'btn btn-sm btn-outline-secondary d-block mb-2';
      const result = node('div');
      button.addEventListener('click', async () => {
        try {const response = await fetch('/imports/' + item.batch_id); const data = await response.json(); if (!response.ok) throw new Error(data.detail); render(result, data);}
        catch (error) {result.textContent = error.message;}
      });
      history.append(button, result);
    }
    if (!data.items.length) history.append(node('p', 'No imports recorded since provenance tracking was enabled. Existing holdings retain legacy provenance.'));
  };
  const refresh = document.getElementById('import-history-refresh');
  if (refresh) refresh.addEventListener('click', () => load().catch(error => {history.textContent = error.message;}));
  const prices = document.getElementById('account-prices-refresh');
  if (prices) prices.addEventListener('click', async () => {
    prices.disabled = true;
    const status = document.getElementById('account-prices-status');
    status.textContent = 'Matching investments and refreshing prices…';
    try {
      const response = await fetch('/imports/pricing/work');
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Price refresh failed. Retry later.');
      status.replaceChildren(); const progress = node('p'); status.append(progress);
      let done = 0, fetched = 0, matched = 0;
      for (const item of data.items) {
        progress.textContent = `Matching and pricing ${++done} of ${data.items.length}: ${item.ticker}`;
        const response = await fetch('/imports/attention/auto', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(item)});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || 'Refresh interrupted. Completed matches and quotes are saved; retry to continue.');
        if (result.status === 'matched') matched++;
        if (result.pricing?.status === 'fetched') fetched++;
        if (result.status === 'needs_attention' || result.pricing?.status === 'unavailable') {
          status.append(node('p', `${item.ticker}: ${result.pricing?.reason || result.reason}`));
        }
      }
      progress.textContent = `${fetched} account quotes refreshed; ${matched} investments matched automatically. Current verified quotes are reused.`;
    } catch (error) {status.textContent = error.message;}
    finally {prices.disabled = false;}
  });
})();
