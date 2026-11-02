(() => {
  'use strict';
  const node = (tag, text) => {const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el;};
  const button = (label, handler) => {const el = node('button', label); el.type = 'button'; el.className = 'btn btn-sm btn-outline-primary me-2 mt-2'; el.addEventListener('click', handler); return el;};
  const request = async (path, body) => {
    const response = await fetch('/imports/attention/' + path, body === undefined ? undefined : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(typeof result.detail === 'string' ? result.detail : 'Unable to complete this action. Check the details and retry.');
      error.status = response.status; throw error;
    }
    return result;
  };
  function renderItem(container, item) {
    const card = node('div'); card.className = 'border rounded p-3 mb-2'; container.append(card);
    card.append(node('strong', `${item.ticker} · ${item.account_name}`), node('p', item.reason));
    const scope = {platform_id: item.platform_id, ticker: item.ticker};
    const status = node('p'); status.setAttribute('role', 'status'); card.append(status);
    const refresh = button('Refresh this price', async () => {
      refresh.disabled = true;
      try {const result = await request('refresh', scope); status.textContent = result.reason;}
      catch (error) {status.textContent = error.message;}
      finally {refresh.disabled = false;}
    });
    if (item.can_refresh) card.append(refresh);
    if (!item.can_review) return;
    const details = node('details'); details.append(node('summary', item.can_refresh ? 'Review or change the match' : 'Review this investment'));
    card.append(details);
    const controls = [], fields = {};
    const preview = node('div'); let token = null, generation = 0;
    const invalidate = () => {token = null; generation++; preview.replaceChildren();};
    const field = (key, label, value = '') => {
      const wrapper = node('label', label); wrapper.className = 'd-block mt-2';
      const input = node('input'); input.type = 'text'; input.value = value || ''; input.className = 'form-control form-control-sm';
      input.maxLength = key === 'isin' ? 12 : key === 'symbol' ? 40 : key === 'mic' ? 4 : 300;
      input.addEventListener('input', invalidate); input.addEventListener('change', invalidate);
      wrapper.append(input); details.append(wrapper); fields[key] = input; controls.push(input);
    };
    field('isin', 'ISIN from your statement', item.isin);
    field('currency', 'Quote units from your statement (e.g. GBP, GBX for pence, USD)', item.currency);
    field('mic', 'Exchange MIC, if supplied (optional)', item.mic);
    field('statement_reference', 'Statement date or reference');
    field('symbol', 'Pricing symbol (optional; leave blank to find listings)');
    if (item.hints?.isin || item.hints?.symbol) {
      details.append(node('p', `Catalogue hints — confirm against your statement: ${item.hints.isin || 'ISIN unknown'} · ${item.hints.currency || 'quote units unknown'} · ${item.hints.symbol || 'symbol unknown'}`));
      details.append(button('Copy catalogue hints', () => {
        if (!fields.isin.value) fields.isin.value = item.hints.isin || '';
        if (!fields.currency.value) fields.currency.value = item.hints.currency || '';
        if (!fields.symbol.value) fields.symbol.value = item.hints.symbol || '';
        invalidate();
      }));
    }
    const label = node('label', ' These identity details match my statement.'); label.className = 'd-block mt-2';
    const confirmed = node('input'); confirmed.type = 'checkbox'; confirmed.addEventListener('change', invalidate);
    label.prepend(confirmed); details.append(label); controls.push(confirmed);
    const verify = button('Verify investment', async () => {
      invalidate();
      if (!confirmed.checked) {status.textContent = 'Confirm the identity details against your statement first.'; return;}
      const version = generation;
      verify.disabled = true; status.textContent = 'Verifying listings…';
      try {
        const payload = {...scope, confirmed: confirmed.checked};
        for (const [key, input] of Object.entries(fields)) payload[key] = input.value.trim();
        const result = await request('verify', payload);
        if (version !== generation) return;
        status.textContent = result.message || (result.can_apply ? 'Verified. Review this listing before confirming.' : 'The listing did not pass verification.');
        for (const candidate of result.candidates || []) {
          preview.append(button(`${candidate.symbol} · ${candidate.exchange} · ${candidate.currency}`, () => {
            fields.symbol.value = candidate.symbol; invalidate();
            status.textContent = 'Listing selected. Press Verify investment to review this choice.';
          }));
        }
        if (result.candidate) {
          const c = result.candidate;
          preview.append(node('p', `${c.name || ''} · ${c.symbol} · ${c.isin || 'ISIN unavailable'} · ${c.exchange || ''} · ${c.currency || ''}`));
          preview.append(node('p', `Quote: ${c.price ?? 'unavailable'} ${c.currency || ''} · ${c.observed_at || ''}`));
          if (!result.can_apply) for (const reason of c.evidence || []) preview.append(node('p', reason));
        }
        if (result.can_apply && result.review_token) {
          token = result.review_token;
          const apply = button('Confirm match and refresh price', async () => {
            if (!token) return;
            const reviewedToken = token; token = null; apply.disabled = true;
            verify.disabled = true;
            for (const input of controls) input.disabled = true;
            try {
              const saved = await request('apply', {review_token: reviewedToken, reviewed: true});
              preview.replaceChildren(node('p', `Match saved. ${saved.pricing?.reason || 'Price refresh completed.'}`));
              if (saved.pricing?.status === 'unavailable') preview.append(refresh);
              status.textContent = 'Purchase costs and transaction history are unchanged.';
            } catch (error) {
              if ([400, 401, 403, 404, 409, 422].includes(error.status)) {
                invalidate(); status.textContent = error.message + ' Verify the investment again before applying.';
              } else {status.textContent = error.message; token = reviewedToken; apply.disabled = false;}
            } finally {for (const input of controls) input.disabled = false; verify.disabled = false;}
          });
          preview.append(apply);
        }
      } catch (error) {status.textContent = error.message;}
      finally {verify.disabled = false;}
    });
    details.append(verify, preview);
  }
  window.HoldingAttention = {renderItem};
  const load = document.getElementById('holding-attention-load');
  if (!load) return;
  const results = document.getElementById('holding-attention-results');
  const search = document.getElementById('holding-attention-search');
  const status = document.getElementById('holding-attention-status');
  let offset = 0, total = 0;
  const fetchPage = async () => {
    load.disabled = true; status.textContent = 'Loading holdings that need attention…';
    try {
      let data = await request(`list?search=${encodeURIComponent(search.value)}&offset=${offset}`);
      let repaired = 0;
      for (const item of data.items) {
        status.textContent = `Automatically verifying ${item.ticker} by ISIN…`;
        try {
          const result = await request('auto', {platform_id: item.platform_id, ticker: item.ticker});
          if (result.pricing && result.pricing.status !== 'unavailable') repaired++;
          else if (result.reason) item.reason = result.reason;
          if (result.pricing?.status === 'unavailable') item.reason = result.pricing.reason;
        } catch (error) {item.reason = error.message;}
      }
      if (repaired) {
        offset = 0;
        data = await request(`list?search=${encodeURIComponent(search.value)}&offset=0`);
      }
      total = data.total; results.replaceChildren();
      for (const item of data.items) renderItem(results, item);
      status.textContent = `${repaired} prices restored automatically. ` + (total ? `${total} holdings need attention. Showing ${offset + 1}–${offset + data.items.length}.` : 'No current unpriced holdings need attention.');
    } catch (error) {status.textContent = error.message;}
    finally {load.disabled = false;}
  };
  load.addEventListener('click', () => {offset = 0; fetchPage();});
  document.getElementById('holding-attention-prev').addEventListener('click', () => {offset = Math.max(0, offset - 25); fetchPage();});
  document.getElementById('holding-attention-next').addEventListener('click', () => {if (offset + 25 < total) {offset += 25; fetchPage();}});
})();
